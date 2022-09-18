from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from logging import CRITICAL
from operator import attrgetter, methodcaller
import operator as O
import pdb
from shutil import ignore_patterns
from typing import Any, Callable, Collection, List, Union, Tuple, Optional
from typing_extensions import Self

import numpy as np
import pandas as pd
import pyspark.sql.functions as F
import toolz as Z
from loguru import logger
from pyspark.sql import SparkSession, DataFrame, Observation, Column
from pyspark.sql import Window as W
import uuid
import inspect
import sys
import enum
import itertools
from . import dataframe as D
import hashlib


class CheckLevel(enum.Enum):
    WARNING = 0
    ERROR = 1


class CheckTag(enum.Enum):
    AGNOSTIC = 0
    NUMERIC = 1
    STRING = 2
    DATE = 3
    TIME = 4


@dataclass(frozen=True)
class Rule:
    method: str
    column: List[str]
    value: Optional[Any]
    expression: str
    tag: str
    coverage: float = 1.0

    def __repr__(self):
        return f"Rule(method:{self.method}, column:{self.column}, tag:{self.tag}, coverage:{self.coverage})"

def _single_value_rule(
        method: str,
        column: str,
        value: Optional[Any],
        operator: Callable,
        tag: CheckTag,
        coverage: float
    ):
        key = hashlib.md5(bytes(f"{method}_{column}", "UTF8")).hexdigest()
        return Rule(
            method=method, 
            column=column, 
            value=value, 
            expression=lambda rows, expectation: (
                    (F.sum((operator(F.col(column), value)).cast("integer")) / F.lit(rows))
                    >= F.lit(expectation)
                ).alias(key)
            
            ,
            tag=tag,
            coverage=coverage
        )

class Check:
    def __init__(self, level: CheckLevel, description: str):
        self._rules = []
        self.level = level
        self.description = description

    def is_complete(self, column: str) -> Self:
        """Validation for non-null values in column"""
        self._rules.append(Rule("is_complete", column))
        return self

    def is_unique(self, column: str) -> Self:
        """Validation for unique values in column"""
        self._rules.append(Rule("is_unique", column))
        return self

    def is_greater_than(self, column: str, value: float, pct: float = 1.0) -> Self:
        """Validation for numeric greater than value"""

        self._rules.append(
            _single_value_rule("is_greater_than", column, value, O.gt, CheckTag.NUMERIC, pct)
        )
        return self

    def is_greater_or_equal_than(self, column: str, value: float, pct: float = 1.0) -> Self:
        """Validation for numeric greater or equal than value"""

        self._rules.append(
            _single_value_rule("is_greater_or_equal_than", column, value, O.ge, CheckTag.NUMERIC, pct)
        )
        return self

    def is_less_than(self, column: str, value: float, pct: float = 1.0) -> Self:
        """Validation for numeric less than value"""
        self._rules.append(
            _single_value_rule("is_less_than", column, value, O.lt, CheckTag.NUMERIC, pct)
        )
        return self

    def is_less_or_equal_than(self, column: str, value: float, pct: float = 1.0) -> Self:
        """Validation for numeric less or equal than value"""
        self._rules.append(
            _single_value_rule("is_less_or_equal_than", column, value, O.le, CheckTag.NUMERIC, pct)
        )

        return self

    def is_equal_than(self, column: str, value: float, pct: float = 1.0) -> Self:
        """Validation for numeric column equal than value"""

        self._rules.append(
            _single_value_rule("is_equal_than", column, value, O.eq, CheckTag.NUMERIC, pct)
        )
        return self

    def has_min(self, column: str, value: float, pct: float = 1.0) -> Self:
        """
        Validation for numeric greater than value
        ƒ: F.sum((F.col(column) > value).cast('integer'))

        """
        self._rules.append(
            _single_value_rule("is_less_than", column, value, O.lt, CheckTag.NUMERIC, pct)
        )

        return self

    # HERE Thank you!
    # ==============
    def has_min(self, column: str, value: float) -> Self:
        """Validation of a column’s minimum value"""
        self._rules.append(Rule("has_min", column, value, CheckTag.NUMERIC))
        return self
        # Calculation:
        # assert F.min(F.col(column)) == value

    def has_max(self, column: str, value: float) -> Self:
        """Validation of a column’s maximu value"""
        pass

    # ==============

    def __repr__(self):
        return f"Check(level:{self.level}, desc:{self.description}, rules:{len(self._rules)})"

    def validate(self, dataframe: DataFrame):
        """Compute all rules in this check for specific data frame"""
        assert (
            self._rules
        ), "Check is empty. Add validations i.e. is_complete, is_unique, etc."

        assert isinstance(
            dataframe, DataFrame
        ), "Cualle operates only with Spark Dataframes"

        # Pre-validate columns
        rule_set = set(self._rules)
        column_set = set(map(attrgetter("column"), rule_set))
        unknown_columns = column_set.difference(dataframe.columns)
        assert column_set.issubset(
            dataframe.columns
        ), f"Column(s): {unknown_columns} not in dataframe"

        # Pre-Validation of numeric data types
        numeric_rules = set([r.column for r in rule_set if r.tag == CheckTag.NUMERIC])
        numeric_fields = D.numeric_fields(dataframe)
        non_numeric_columns = numeric_rules.difference(numeric_fields)
        assert set(numeric_rules).issubset(
            numeric_fields
        ), f"Column(s): {non_numeric_columns} are not numeric"

        rows = dataframe.count()
        return dataframe.select(*[r.expression(rows, r.coverage) for r in rule_set])



def completeness(dataframe: DataFrame) -> float:
    """Percentage of filled values against total number of fields = fields_filled / (rows x cols)"""
    _hash = dataframe.semanticHash()
    observe_completeness = Observation("completeness")
    _pk = f"rows_{_hash}"
    _cols = dataframe.columns
    _nulls = [
        F.sum(F.col(column).isNotNull().cast("long")).alias(f"{column}")
        for column in _cols
    ]
    _rows = [F.count(F.lit(1)).alias(_pk)]
    _null_cols = _nulls + _rows
    dataframe.observe(observe_completeness, *_null_cols).count()

    results = observe_completeness.get
    rows = results.pop(_pk)
    _shape = len(_cols) * rows
    return round(sum(results.values()) / _shape, 6)


