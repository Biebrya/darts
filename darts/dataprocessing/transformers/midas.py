"""
Mixed-data sampling (MIDAS) Transformer
------------------
"""
from typing import Any, Mapping

import numpy as np
import pandas as pd
from pandas import DatetimeIndex

from darts import TimeSeries
from darts.dataprocessing.transformers import InvertibleDataTransformer
from darts.logging import get_logger, raise_if, raise_if_not

logger = get_logger(__name__)


class MIDAS(InvertibleDataTransformer):
    def __init__(
        self,
        rule: str,
        strip: bool = False,
        name: str = "MIDASTransformer",
        n_jobs: int = 1,
        verbose: bool = False,
    ):
        """Mixed-data sampling transformer.

        A transformer that converts higher frequency time series to lower frequency using mixed-data sampling; see
        [1]_ for further details. This allows higher frequency covariates to be used whilst forecasting a lower
        frequency target series. For example, using monthly inputs to forecast a quarterly target.

        Notes
        -----
        The high input frequency should always relate in the same rate to the low target frequency. For
        example, there's always three months in quarter. However, the number of days in a month varies per month. So in
        the latter case a MIDAS transformation does not work and the transformer will raise an error.

        Parameters
        ----------
        rule
            The offset string or object representing target conversion. Passed on to the rule parameter in
            pandas.DataFrame.resample and therefore it is equivalent to it.
        strip
            Whether to strip -remove the NaNs from the start and the end of- the transformed series.

        Examples
        --------
        >>> from darts.datasets import AirPassengersDataset
        >>> from darts.dataprocessing.transformers import MIDAS
        >>> monthly_series = AirPassengersDataset().load()
        >>> midas = MIDAS(rule="QS")
        >>> quarterly_series = midas.transform(monthly_series)
        >>> print(quarterly_series.head())
        <TimeSeries (DataArray) (Month: 5, component: 3, sample: 1)>
        array([[[112.],
                [118.],
                [132.]],
        <BLANKLINE>
               [[129.],
                [121.],
                [135.]],
        <BLANKLINE>
               [[148.],
                [148.],
                [136.]],
        <BLANKLINE>
               [[119.],
                [104.],
                [118.]],
        <BLANKLINE>
               [[115.],
                [126.],
                [141.]]])
        Coordinates:
          * Month      (Month) datetime64[ns] 1949-01-01 1949-04-01 ... 1950-01-01
          * component  (component) object '#Passengers_0' ... '#Passengers_2'
        Dimensions without coordinates: sample
        Attributes:
            static_covariates:  None
            hierarchy:          None

        References
        ----------
        .. [1] https://en.wikipedia.org/wiki/Mixed-data_sampling
        """
        self._rule = rule
        self._strip = strip
        super().__init__(name, n_jobs, verbose)

    @staticmethod
    def ts_transform(series: TimeSeries, params: Mapping[str, Any]) -> TimeSeries:
        """
        Transforms series from high to low frequency using a mixed-data sampling approach. Uses and relies on
        pandas.DataFrame.resample.

        Steps:
            (1) Transform series to pd.DataFrame and get frequency string for PeriodIndex
            (2) Downsample series and then upsample it again
            (3) Replace input series by unsampled series if it's not 'full'
            (4) Transform every column of the high frequency series into multiple columns for the low frequency series
            (5) Transform the low frequency series back into a TimeSeries
        """
        # MIDAS is non-invertible for multivariate series
        MIDAS._verify_series(series)

        rule, strip = params["fixed"]["_rule"], params["fixed"]["_strip"]
        high_freq_datetime = series.freq_str

        # TimeSeries to pd.DataFrame
        series_df = series.pd_dataframe(copy=True)
        # TODO: get ride of the double copy?
        series_copy_df = series_df.copy()

        # get high frequency string that's suitable for PeriodIndex
        high_freq_period = series_df.index.to_period().freqstr

        # downsample
        low_freq_series_df = series_df.resample(rule).last()
        # save the downsampled index
        low_index_datetime = low_freq_series_df.index

        # upsample again to get full range of high freq periods for every low freq period
        low_freq_series_df.index = low_index_datetime.to_period()
        high_freq_series_df = low_freq_series_df.resample(high_freq_period).last()

        # make sure the extension of the index matches the original index
        if "End" in str(series.freq):
            args_to_timestamp = {"freq": high_freq_period}
        else:
            args_to_timestamp = {"how": "start"}
        high_index_datetime = high_freq_series_df.index.to_timestamp(
            **args_to_timestamp
        )

        raise_if_not(
            low_freq_series_df.shape[0] < high_freq_series_df.shape[0],
            f"The target conversion should go from a high to a "
            f"low frequency, instead the targeted frequency is "
            f"{rule}, while the original frequency is {high_freq_datetime}.",
            logger,
        )

        # if necessary, expand the original series
        if len(high_index_datetime) > series_df.shape[0]:
            series_df = pd.DataFrame(
                np.nan, index=high_index_datetime, columns=series_copy_df.columns
            )
            series_df.loc[series_copy_df.index, :] = series_copy_df.values

        # make multiple low frequency columns out of the high frequency column(s)
        midas_df = _create_midas_df(
            series_df=series_df,
            low_index_datetime=low_index_datetime,
        )

        # back to TimeSeries
        midas_ts = TimeSeries.from_dataframe(
            midas_df,
            static_covariates=series.static_covariates,
        )
        if strip:
            midas_ts = midas_ts.strip()

        return midas_ts

    @staticmethod
    def ts_inverse_transform(
        series: TimeSeries, params: Mapping[str, Any]
    ) -> TimeSeries:
        """
        Transforms series back to high frequency
        """
        MIDAS._verify_series(series, check_multivariate=False)

        start_time = series.start_time()
        # extract array from ts & flatten components dimension
        series_values = series.values().flatten()

        # retrieve the original high-freq from the number of components
        n_components = series.n_components
        low_freq_name = series.freq_str

        # cannot be reversed numerically from the low-freq offset
        if "Q" in low_freq_name:
            high_freq_name = "M"
            # correct the shift when necessary
            if "S" not in low_freq_name:
                start_time -= pd.DateOffset(months=n_components - 1)
        else:
            # low_timedelta: pd.Timedelta = series.time_index[1] - series.time_index[0]
            low_timedelta = pd.Timedelta(value=1, unit=low_freq_name)
            high_timedelta = low_timedelta / n_components
            high_freq_name = high_timedelta.resolution_string

        new_times = pd.date_range(
            start=start_time, periods=len(series_values), freq=high_freq_name
        )

        # retrieve component name
        component_name = series.components[-1][: -len(f"_{n_components}")]

        return TimeSeries.from_times_and_values(
            times=new_times,
            values=series_values,
            freq=high_freq_name,
            columns=[component_name],
            static_covariates=series.static_covariates,
        )

    @staticmethod
    def _verify_series(series: TimeSeries, check_multivariate: bool = True):
        raise_if(
            series.is_probabilistic,
            "MIDAS Transformer cannot be applied to probabilistic/stochastic TimeSeries",
            logger,
        )

        raise_if_not(
            isinstance(series.time_index, pd.DatetimeIndex),
            "MIDAS input series must have a pd.Datetime index",
            logger,
        )

        if check_multivariate:
            raise_if(
                series.n_components > 1,
                "MIDAS Transformer cannot be applied to multivariate TimeSeries",
                logger,
            )


def _create_midas_df(
    series_df: pd.DataFrame,
    low_index_datetime: DatetimeIndex,
) -> pd.DataFrame:
    """
    Function creating the lower frequency dataframe out of a higher frequency dataframe.
    """
    # calculate the multiple
    n_high = series_df.shape[0]
    n_low = len(low_index_datetime)
    multiple = n_high / n_low

    raise_if_not(
        multiple.is_integer(),
        "The frequency of the high frequency input series should be an exact multiple of the targeted"
        "low frequency output. For example, you could go from a monthly series to a quarterly series.",
        logger,
    )

    multiple = int(multiple)

    # set up integer index
    range_lst = list(range(n_high))
    col_names = list(series_df.columns)
    midas_lst = []

    # for every column we now create 'multiple' columns
    # by going through a column and picking every one in 'multiple' values
    for f in range(multiple):
        range_lst_tmp = range_lst[f:][0::multiple]
        series_tmp_df = series_df.iloc[range_lst_tmp, :]
        series_tmp_df.index = low_index_datetime
        col_names_tmp = [col_name + f"_{f}" for col_name in col_names]
        rename_dict_tmp = dict(zip(col_names, col_names_tmp))
        midas_lst += [series_tmp_df.rename(columns=rename_dict_tmp)]

    return pd.concat(midas_lst, axis=1)
