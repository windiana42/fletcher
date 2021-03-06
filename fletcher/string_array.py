import math
import types
from typing import Optional, Union

import numba
import numba.experimental
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from fletcher._algorithms import _extract_isnull_bitmap
from fletcher.algorithms.bool import all_true_like
from fletcher.algorithms.string import (
    _endswith,
    _startswith,
    _text_cat,
    _text_cat_chunked,
    _text_cat_chunked_mixed,
    _text_contains_case_sensitive,
)
from fletcher.base import (
    FletcherBaseArray,
    FletcherChunkedArray,
    FletcherContinuousArray,
)


def buffers_as_arrays(sa):
    buffers = sa.buffers()
    return (
        _extract_isnull_bitmap(sa, 0, len(sa)),
        np.asarray(buffers[1]).view(np.uint32),
        np.asarray(buffers[2]).view(np.uint8),
    )


@numba.experimental.jitclass(
    [
        ("missing", numba.uint8[:]),
        ("offsets", numba.uint32[:]),
        ("data", numba.optional(numba.uint8[:])),
        ("offset", numba.int64),
    ]
)
class NumbaStringArray:
    """Wrapper around arrow's StringArray for use in numba functions.

    Usage::

        NumbaStringArray.make(array)
    """

    def __init__(self, missing, offsets, data, offset):
        self.missing = missing
        self.offsets = offsets
        self.data = data
        self.offset = offset

    @property
    def byte_size(self):
        # TODO: offset?
        return self.data.shape[0]

    @property
    def size(self):
        return len(self.offsets) - 1 - self.offset

    def isnull(self, str_idx):
        str_idx += self.offset
        byte_idx = str_idx // 8
        bit_mask = 1 << (str_idx % 8)
        return (self.missing[byte_idx] & bit_mask) == 0

    def byte_length(self, str_idx):
        str_idx += self.offset
        return self.offsets[str_idx + 1] - self.offsets[str_idx]

    def get_byte(self, str_idx, byte_idx):
        str_idx += self.offset
        full_idx = self.offsets[str_idx] + byte_idx
        return self.data[full_idx]

    def length(self, str_idx):
        result = 0
        byte_length = self.byte_length(str_idx)
        current = 0

        while current < byte_length:
            _, inc = self.get(str_idx, current)
            current += inc
            result += 1

        return result

    # TODO: implement this
    def get(self, str_idx, byte_idx):
        b = self.get_byte(str_idx, byte_idx)
        if b > 127:
            raise ValueError()

        return b, 1

    def decode(self, str_idx):
        byte_length = self.byte_length(str_idx)
        buffer = np.zeros(byte_length, np.int32)

        i = 0
        j = 0
        while i < byte_length:
            code, inc = self.get(str_idx, i)
            buffer[j] = code

            i += inc
            j += 1

        return buffer[:j]


def _make(cls, sa):
    if not isinstance(sa, pa.StringArray):
        sa = pa.array(sa, pa.string())

    return cls(*buffers_as_arrays(sa), offset=sa.offset)


# @classmethod does not seem to be supported
NumbaStringArray.make = types.MethodType(_make, NumbaStringArray)  # type: ignore


@numba.experimental.jitclass(
    [("start", numba.uint32), ("end", numba.uint32), ("data", numba.uint8[:])]
)
class NumbaString:
    def __init__(self, data, start=0, end=None):
        if end is None:
            end = data.shape[0]

        self.data = data
        self.start = start
        self.end = end

    @property
    def length(self):
        return self.end - self.start

    def get_byte(self, i):
        return self.data[self.start + i]


def _make_string(cls, obj):
    if isinstance(obj, str):
        data = obj.encode("utf8")
        data = np.asarray(memoryview(data))

        return cls(data, 0, len(data))

    raise TypeError()


NumbaString.make = types.MethodType(_make_string, NumbaString)  # type: ignore


@numba.experimental.jitclass(
    [
        ("missing", numba.uint8[:]),
        ("offsets", numba.uint32[:]),
        ("data", numba.optional(numba.uint8[:])),
        ("string_position", numba.uint32),
        ("byte_position", numba.uint32),
        ("string_capacity", numba.uint32),
        ("byte_capacity", numba.uint32),
    ]
)
class NumbaStringArrayBuilder:
    def __init__(self, string_capacity, byte_capacity):
        self.missing = np.ones(_missing_capactiy(string_capacity), np.uint8)
        self.offsets = np.zeros(string_capacity + 1, np.uint32)
        self.data = np.zeros(byte_capacity, np.uint8)
        self.string_position = 0
        self.byte_position = 0

        self.string_capacity = string_capacity
        self.byte_capacity = byte_capacity

    def increase_string_capacity(self, string_capacity):
        assert string_capacity > self.string_capacity

        missing = np.zeros(_missing_capactiy(string_capacity), np.uint8)
        missing[: _missing_capactiy(self.string_capacity)] = self.missing
        self.missing = missing

        offsets = np.zeros(string_capacity + 1, np.uint32)
        offsets[: self.string_capacity + 1] = self.offsets
        self.offsets = offsets

        self.string_capacity = string_capacity

    def increase_byte_capacity(self, byte_capacity):
        assert byte_capacity > self.byte_capacity

        data = np.zeros(byte_capacity, np.uint8)
        data[: self.byte_capacity] = self.data
        self.data = data

        self.byte_capacity = byte_capacity

    def put_byte(self, b):
        if self.byte_position >= self.byte_capacity:
            self.increase_byte_capacity(int(math.ceil(1.2 * self.byte_capacity)))

        self.data[self.byte_position] = b
        self.byte_position += 1

    def finish_string(self):
        if self.string_position >= self.string_capacity:
            self.increase_string_capacity(int(math.ceil(1.2 * self.string_capacity)))

        self.offsets[self.string_position + 1] = self.byte_position

        byte_idx = self.string_position // 8
        self.missing[byte_idx] |= 1 << (self.string_position % 8)

        self.string_position += 1

    def finish_null(self):
        if self.string_position >= self.string_capacity:
            self.increase_string_capacity(int(math.ceil(1.2 * self.string_capacity)))

        self.offsets[self.string_position + 1] = self.byte_position

        byte_idx = self.string_position // 8
        self.missing[byte_idx] &= ~(1 << (self.string_position % 8))

        self.string_position += 1

    def finish(self):
        self.missing = self.missing[: _missing_capactiy(self.string_position)]
        self.offsets = self.offsets[: self.string_position + 1]
        self.data = self.data[: self.byte_position]


@numba.jit
def _missing_capactiy(capacity):
    return int(math.ceil(capacity / 8))


@pd.api.extensions.register_series_accessor("fr_text")
@pd.api.extensions.register_series_accessor("text")
class TextAccessor:
    """Accessor for pandas exposed as ``.str``."""

    def __init__(self, obj):
        if not isinstance(obj.values, FletcherBaseArray):
            raise AttributeError(
                "only Fletcher{Continuous,Chunked}Array[string] has text accessor"
            )
        self.obj = obj
        self.data = self.obj.values.data

    def cat(self, others: Optional[FletcherBaseArray]) -> pd.Series:
        """
        Concatenate strings in the Series/Index with given separator.

        If `others` is specified, this function concatenates the Series/Index
        and elements of `others` element-wise.
        If `others` is not passed, then all values in the Series/Index are
        concatenated into a single string with a given `sep`.
        """
        if not isinstance(others, pd.Series):
            raise NotImplementedError(
                "other needs to be Series of Fletcher{Chunked,Continuous}Array"
            )
        elif isinstance(others.values, FletcherChunkedArray):
            return pd.Series(
                FletcherChunkedArray(_text_cat_chunked(self.data, others.values.data))
            )
        elif not isinstance(others.values, FletcherContinuousArray):
            raise NotImplementedError("other needs to be FletcherContinuousArray")

        if isinstance(self.obj.values, FletcherChunkedArray):
            return pd.Series(
                FletcherChunkedArray(
                    _text_cat_chunked_mixed(self.data, others.values.data)
                )
            )
        else:  # FletcherContinuousArray
            return pd.Series(
                FletcherContinuousArray(_text_cat(self.data, others.values.data))
            )

    def _call_str_accessor(self, func, *args, **kwargs) -> pd.Series:
        pd_series = self.data.to_pandas()
        return self._series_like(
            pa.array(getattr(pd_series.str, func)(*args, **kwargs).values)
        )

    def _series_like(self, array: Union[pa.Array, pa.ChunkedArray]) -> pd.Series:
        """Return an Arrow result as a series with the same base classes as the input."""
        return pd.Series(
            type(self.obj.values)(array),
            dtype=type(self.obj.dtype)(array.type),
            index=self.obj.index,
        )

    def contains(self, pat: str, case: bool = True, regex: bool = True) -> pd.Series:
        """
        Test if pattern or regex is contained within a string of a Series or Index.

        Return boolean Series or Index based on whether a given pattern or regex is
        contained within a string of a Series or Index.

        This implementation differs to the one in ``pandas``:
         * We always return a missing for missing data.
         * You cannot pass flags for the regular expression module.

        Parameters
        ----------
        pat : str
            Character sequence or regular expression.
        case : bool, default True
            If True, case sensitive.
        regex : bool, default True
            If True, assumes the pat is a regular expression.

            If False, treats the pat as a literal string.

        Returns
        -------
        Series or Index of boolean values
            A Series or Index of boolean values indicating whether the
            given pattern is contained within the string of each element
            of the Series or Index.
        """
        if not regex:
            if len(pat) == 0:
                # For an empty pattern return all-True array
                return self._series_like(all_true_like(self.data))

            if case:
                contains_exact = getattr(
                    pc, "binary_contains_exact", _text_contains_case_sensitive
                )
                # Can just check for a match on the byte-sequence
                return self._series_like(contains_exact(self.data, pat))
            else:
                # Check if pat is all-ascii, then use lookup-table for lowercasing
                # else: use libutf8proc
                pass
        return self._call_str_accessor("contains", pat=pat, case=case, regex=regex)

    def zfill(self, width: int) -> pd.Series:
        """Pad strings in the Series/Index by prepending '0' characters."""
        return self._call_str_accessor("zfill", width)

    def startswith(self, pat):
        """Check whether a row starts with a certain pattern."""
        return self._call_x_with(_startswith, pat)

    def endswith(self, pat):
        """Check whether a row ends with a certain pattern."""
        return self._call_x_with(_endswith, pat)

    def _call_x_with(self, impl, needle, na=None):
        needle = NumbaString.make(needle)  # type: ignore
        result = np.zeros(len(self.data), dtype=np.uint8)

        if isinstance(self.data, pa.ChunkedArray):
            offset = 0
            for chunk in self.data.chunks:
                str_arr = NumbaStringArray.make(chunk)  # type: ignore
                impl(str_arr, needle, 2, offset, result)
                offset += len(chunk)
        else:
            str_arr = NumbaStringArray.make(self.data)  # type: ignore
            impl(str_arr, needle, 2, 0, result)

        return pd.Series(
            type(self.obj.values)(pa.array(result.astype(bool), mask=(result == 2)))
        )
