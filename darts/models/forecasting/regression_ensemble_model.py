"""
Regression ensemble model
-------------------------

An ensemble model which uses a regression model to compute the ensemble forecast.
"""
from typing import List, Optional, Sequence, Tuple, Union

from darts.logging import get_logger, raise_if, raise_if_not
from darts.models.forecasting.ensemble_model import EnsembleModel
from darts.models.forecasting.forecasting_model import ForecastingModel
from darts.models.forecasting.linear_regression_model import LinearRegressionModel
from darts.models.forecasting.regression_model import RegressionModel
from darts.timeseries import TimeSeries
from darts.utils.utils import seq2series, series2seq

logger = get_logger(__name__)


class RegressionEnsembleModel(EnsembleModel):
    def __init__(
        self,
        forecasting_models: List[ForecastingModel],
        regression_train_n_points: int,
        regression_model=None,
        regression_train_num_samples: Optional[int] = 1,
        regression_train_samples_reduction: Optional[Union[str, float]] = "median",
        retrain_forecasting_models: bool = True,
        show_warnings: bool = True,
    ):
        """
        Use a regression model for ensembling individual models' predictions using the stacking technique [1]_.

        The provided regression model must implement ``fit()`` and ``predict()`` methods
        (e.g. scikit-learn regression models). Note that here the regression model is used to learn how to
        best ensemble the individual forecasting models' forecasts. It is not the same usage of regression
        as in :class:`RegressionModel`, where the regression model is used to produce forecasts based on the
        lagged series.

        If `future_covariates` or `past_covariates` are provided at training or inference time,
        they will be passed only to the forecasting models supporting them.

        If `forecasting_models` contains exclusively GlobalForecastingModels, they can be pre-trained. Otherwise,
        the `forecasting_models` must be untrained.

        The regression model does not leverage the covariates passed to ``fit()`` and ``predict()``.

        Parameters
        ----------
        forecasting_models
            List of forecasting models whose predictions to ensemble
        regression_train_n_points
            The number of points to use to train the regression model. Can be set to `-1` to use the entire series
            to train the regressor if `forecasting_models` are already fitted and `retrain_forecasting_models=False`.
        regression_model
            Any regression model with ``predict()`` and ``fit()`` methods (e.g. from scikit-learn)
            Default: ``darts.model.LinearRegressionModel(fit_intercept=False)``

            .. note::
                if `regression_model` is probabilistic, the `RegressionEnsembleModel` will also be probabilistic.
            ..
        regression_train_num_samples
            Number of prediction samples from each forecasting model to train the regression model (samples are
            averaged). Should be set to 1 for deterministic models. Default: 1.

            .. note::
                if `forecasting_models` contains a mix of probabilistic and deterministic models,
                `regression_train_num_samples will be passed only to the probabilistic ones.
            ..
        regression_train_samples_reduction
            If `forecasting_models` are probabilistic and `regression_train_num_samples` > 1, method used to
            reduce the samples before passing them to the regression model. Possible values: "mean", "median"
            or float value corresponding to the desired quantile. Default: "median"
        retrain_forecasting_models
            If set to `False`, the `forecasting_models` are not retrained when calling `fit()` (only supported
            if all the `forecasting_models` are pretrained `GlobalForecastingModels`). Default: ``True``.
        show_warnings
            Whether to show warnings related to forecasting_models covariates support.
        References
        ----------
        .. [1] D. H. Wolpert, “Stacked generalization”, Neural Networks, vol. 5, no. 2, pp. 241–259, Jan. 1992
        """
        super().__init__(
            forecasting_models=forecasting_models,
            train_num_samples=regression_train_num_samples,
            train_samples_reduction=regression_train_samples_reduction,
            retrain_forecasting_models=retrain_forecasting_models,
            show_warnings=show_warnings,
        )

        if regression_model is None:
            regression_model = LinearRegressionModel(
                lags=None, lags_future_covariates=[0], fit_intercept=False
            )
        elif isinstance(regression_model, RegressionModel):
            regression_model = regression_model
        else:
            # scikit-learn like model
            regression_model = RegressionModel(
                lags_future_covariates=[0], model=regression_model
            )

        # check lags of the regression model
        raise_if_not(
            regression_model.lags == {"future": [0]},
            f"`lags` and `lags_past_covariates` of regression model must be `None`"
            f"and `lags_future_covariates` must be [0]. Given:\n"
            f"{regression_model.lags}",
        )

        self.regression_model = regression_model

        raise_if(
            regression_train_n_points == -1
            and not (self.all_trained and (not retrain_forecasting_models)),
            "`regression_train_n_points` can be set to `-1` only if `retrain_forecasting_model=False` and "
            "all the `forecasting_models` are already fitted.",
            logger,
        )

        self.train_n_points = regression_train_n_points

    def _split_multi_ts_sequence(
        self, n: int, ts_sequence: Sequence[TimeSeries]
    ) -> Tuple[Sequence[TimeSeries], Sequence[TimeSeries]]:
        left = [ts[:-n] for ts in ts_sequence]
        right = [ts[-n:] for ts in ts_sequence]
        return left, right

    def fit(
        self,
        series: Union[TimeSeries, Sequence[TimeSeries]],
        past_covariates: Optional[Union[TimeSeries, Sequence[TimeSeries], None]] = None,
        future_covariates: Optional[
            Union[TimeSeries, Sequence[TimeSeries], None]
        ] = None,
    ):
        """
        Fits the forecasting models with the entire series except the last `regression_train_n_points` values, which
        are used to train the regressor model.

        If `forecasting_models` contains fitted `GlobalForecastingModels` and `retrain_forecasting_model=False`, the
        regression model will be trained.

        Parameters
        ----------
        series
            TimeSeries or Sequence[TimeSeries] object containing the target values.
        past_covariates
            Optionally, a series or sequence of series specifying past-observed covariates passed to the
            forecasting models
        future_covariates
            Optionally, a series or sequence of series specifying future-known covariates passed to the
            forecasting models
        """

        super().fit(
            series, past_covariates=past_covariates, future_covariates=future_covariates
        )

        # spare train_n_points points to serve as regression target
        is_single_series = isinstance(series, TimeSeries)
        if self.train_n_points == -1:
            # take the longest possible time index
            self.train_n_points = (
                len(series) if is_single_series else min(len(ts) for ts in series)
            )
            train_n_points_too_big = False
        else:
            if is_single_series:
                train_n_points_too_big = len(series) <= self.train_n_points
            else:
                train_n_points_too_big = any(
                    [len(s) <= self.train_n_points for s in series]
                )

        raise_if(
            train_n_points_too_big,
            "`regression_train_n_points` parameter too big (must be smaller or "
            "equal to the number of points in training_series)",
            logger,
        )

        if is_single_series:
            forecast_training = series[: -self.train_n_points]
            regression_target = series[-self.train_n_points :]
        else:
            forecast_training, regression_target = self._split_multi_ts_sequence(
                self.train_n_points, series
            )

        if self.retrain_forecasting_models:
            for model in self.forecasting_models:
                # maximize covariate usage
                model._fit_wrapper(
                    series=forecast_training,
                    past_covariates=past_covariates
                    if model.supports_past_covariates
                    else None,
                    future_covariates=future_covariates
                    if model.supports_future_covariates
                    else None,
                )

        predictions = self._make_multiple_predictions(
            n=self.train_n_points,
            series=forecast_training,
            past_covariates=past_covariates,
            future_covariates=future_covariates,
            num_samples=self.train_num_samples,
        )

        # train the regression model on the individual models' predictions
        self.regression_model.fit(
            series=regression_target, future_covariates=predictions
        )

        # prepare the forecasting models for further predicting by fitting them with the entire data
        if self.retrain_forecasting_models:
            # Some models may need to be 'reset' to allow being retrained from scratch, especially torch-based models
            self.forecasting_models: List[ForecastingModel] = [
                model.untrained_model() for model in self.forecasting_models
            ]

            for model in self.forecasting_models:
                model._fit_wrapper(
                    series=series,
                    past_covariates=past_covariates
                    if model.supports_past_covariates
                    else None,
                    future_covariates=future_covariates
                    if model.supports_future_covariates
                    else None,
                )
        # update training_series attribute to make predict() behave as expected
        else:
            for model in self.forecasting_models:
                model.training_series = series if is_single_series else None
        return self

    def ensemble(
        self,
        predictions: Union[TimeSeries, Sequence[TimeSeries]],
        series: Optional[Sequence[TimeSeries]] = None,
        num_samples: int = 1,
    ) -> Union[TimeSeries, Sequence[TimeSeries]]:

        is_single_series = isinstance(series, TimeSeries) or series is None
        predictions = series2seq(predictions)
        series = series2seq(series) if series is not None else [None]

        ensembled = [
            self.regression_model.predict(
                n=len(prediction),
                series=serie,
                future_covariates=prediction,
                num_samples=num_samples,
            )
            for serie, prediction in zip(series, predictions)
        ]
        return seq2series(ensembled) if is_single_series else ensembled

    @property
    def extreme_lags(
        self,
    ) -> Tuple[
        Optional[int],
        Optional[int],
        Optional[int],
        Optional[int],
        Optional[int],
        Optional[int],
    ]:
        extreme_lags_ = super().extreme_lags
        return (extreme_lags_[0] - self.train_n_points,) + extreme_lags_[1:]

    def _is_probabilistic(self) -> bool:
        """
        A RegressionEnsembleModel is probabilistic if its regression
        model is probabilistic (ensembling layer)
        """
        return self.regression_model._is_probabilistic()
