# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Linear Estimators."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math

import six

from tensorflow.python.estimator import estimator
from tensorflow.python.estimator.canned import head as head_lib
from tensorflow.python.estimator.canned import optimizers
from tensorflow.python.feature_column import feature_column as feature_column_lib
from tensorflow.python.ops import partitioned_variables
from tensorflow.python.ops import variable_scope
from tensorflow.python.training import ftrl
from tensorflow.python.training import training_util


# The default learning rate of 0.2 is a historical artifact of the initial
# implementation, but seems a reasonable choice.
_LEARNING_RATE = 0.2


def _get_default_optimizer(feature_columns):
  learning_rate = min(_LEARNING_RATE, 1.0 / math.sqrt(len(feature_columns)))
  return ftrl.FtrlOptimizer(learning_rate=learning_rate)


# TODO(b/36813849): Revisit passing params vs named arguments.
def _linear_model_fn(features, labels, mode, params, config):
  """A model_fn for linear models that use a gradient-based optimizer.

  Args:
    features: Dict of `Tensor`.
    labels: `Tensor` of shape `[batch_size, logits_dimension]`.
    mode: Defines whether this is training, evaluation or prediction.
      See `ModeKeys`.
    params: A dict of hyperparameters.
      The following hyperparameters are expected:
      * head: A `Head` instance.
      * feature_columns: An iterable containing all the feature columns used by
          the model.
      * optimizer: string, `Optimizer` object, or callable that defines the
          optimizer to use for training. If `None`, will use a FTRL optimizer.
    config: `RunConfig` object to configure the runtime settings.

  Returns:
    An `EstimatorSpec` instance.

  Raises:
    ValueError: If mode or params are invalid.
  """
  head = params['head']
  feature_columns = tuple(params['feature_columns'])
  optimizer = optimizers.get_optimizer_instance(
      params.get('optimizer') or _get_default_optimizer(feature_columns),
      learning_rate=_LEARNING_RATE)
  num_ps_replicas = config.num_ps_replicas if config else 0

  partitioner = params.get('partitioner') or (
      partitioned_variables.min_max_variable_partitioner(
          max_partitions=num_ps_replicas,
          min_slice_size=64 << 20))

  with variable_scope.variable_scope(
      'linear',
      values=tuple(six.itervalues(features)),
      partitioner=partitioner):

    logits = feature_column_lib.linear_model(
        features=features,
        feature_columns=feature_columns,
        units=head.logits_dimension)

    def _train_op_fn(loss):
      """Returns the op to optimize the loss."""
      return optimizer.minimize(
          loss,
          global_step=training_util.get_global_step())

    return head.create_estimator_spec(
        features=features,
        mode=mode,
        labels=labels,
        train_op_fn=_train_op_fn,
        logits=logits)


class LinearClassifier(estimator.Estimator):
  """Linear classifier model.

  Train a linear model to classify instances into one of multiple possible
  classes. When number of possible classes is 2, this is binary classification.

  Example:

  ```python
  sparse_column_a = sparse_column_with_hash_bucket(...)
  sparse_column_b = sparse_column_with_hash_bucket(...)

  sparse_feature_a_x_sparse_feature_b = crossed_column(...)

  # Estimator using the default optimizer.
  estimator = LinearClassifier(
      feature_columns=[sparse_column_a, sparse_feature_a_x_sparse_feature_b])

  # Or estimator using the FTRL optimizer with regularization.
  estimator = LinearClassifier(
      feature_columns=[sparse_column_a, sparse_feature_a_x_sparse_feature_b],
      optimizer=tf.train.FtrlOptimizer(
        learning_rate=0.1,
        l1_regularization_strength=0.001
      ))

  # Input builders
  def input_fn_train: # returns x, y (where y represents label's class index).
    ...
  def input_fn_eval: # returns x, y (where y represents label's class index).
    ...
  estimator.train(input_fn=input_fn_train)
  estimator.evaluate(input_fn=input_fn_eval)
  estimator.predict(input_fn=input_fn_predict)
  ```

  Input of `train` and `evaluate` should have following features,
    otherwise there will be a `KeyError`:

  * if `weight_feature_key` is not `None`, a feature with
    `key=weight_feature_key` whose value is a `Tensor`.
  * for each `column` in `feature_columns`:
    - if `column` is a `SparseColumn`, a feature with `key=column.name`
      whose `value` is a `SparseTensor`.
    - if `column` is a `WeightedSparseColumn`, two features: the first with
      `key` the id column name, the second with `key` the weight column name.
      Both features' `value` must be a `SparseTensor`.
    - if `column` is a `RealValuedColumn`, a feature with `key=column.name`
      whose `value` is a `Tensor`.
  """

  def __init__(self,
               feature_columns,
               model_dir=None,
               n_classes=2,
               weight_feature_key=None,
               optimizer=None,
               config=None,
               partitioner=None):
    """Construct a `LinearClassifier` estimator object.

    Args:
      feature_columns: An iterable containing all the feature columns used by
        the model. All items in the set should be instances of classes derived
        from `FeatureColumn`.
      model_dir: Directory to save model parameters, graph and etc. This can
        also be used to load checkpoints from the directory into a estimator
        to continue training a previously saved model.
      n_classes: number of label classes. Default is binary classification.
        Note that class labels are integers representing the class index (i.e.
        values from 0 to n_classes-1). For arbitrary label values (e.g. string
        labels), convert to class indices first.
      weight_feature_key: A string defining feature column name representing
        weights. It is used to down weight or boost examples during training. It
        will be multiplied by the loss of the example.
      optimizer: The optimizer used to train the model. If specified, it should
        be either an instance of `tf.Optimizer` or the SDCAOptimizer. If `None`,
        the Ftrl optimizer will be used.
      config: `RunConfig` object to configure the runtime settings.
      partitioner: Optional. Partitioner for input layer.

    Returns:
      A `LinearClassifier` estimator.

    Raises:
      ValueError: if n_classes < 2.
    """
    super(LinearClassifier, self).__init__(
        model_fn=_linear_model_fn,
        model_dir=model_dir,
        config=config,
        params={
            # pylint: disable=protected-access
            # TODO(xiejw): Switch to the classifier head.
            'head': head_lib._regression_head_with_mean_squared_error_loss(
                label_dimension=n_classes,
                weight_feature_key=weight_feature_key),
            # pylint: enable=protected-access
            'feature_columns': feature_columns,
            'optimizer': optimizer,
            'partitioner': partitioner,
        })


class LinearRegressor(estimator.Estimator):
  """An estimator for TensorFlow Linear regression problems.

  Train a linear regression model to predict label value given observation of
  feature values.

  Example:

  ```python
  sparse_column_a = sparse_column_with_hash_bucket(...)
  sparse_column_b = sparse_column_with_hash_bucket(...)

  sparse_feature_a_x_sparse_feature_b = crossed_column(...)

  estimator = LinearRegressor(
      feature_columns=[sparse_column_a, sparse_feature_a_x_sparse_feature_b])

  # Input builders
  def input_fn_train: # returns x, y
    ...
  def input_fn_eval: # returns x, y
    ...
  estimator.train(input_fn=input_fn_train)
  estimator.evaluate(input_fn=input_fn_eval)
  estimator.predict(input_fn=input_fn_predict)
  ```

  Input of `train` and `evaluate` should have following features,
    otherwise there will be a KeyError:

  * if `weight_feature_key` is not `None`:
    key=weight_feature_key, value=a `Tensor`
  * for column in `feature_columns`:
    - if isinstance(column, `SparseColumn`):
        key=column.name, value=a `SparseTensor`
    - if isinstance(column, `WeightedSparseColumn`):
        {key=id column name, value=a `SparseTensor`,
         key=weight column name, value=a `SparseTensor`}
    - if isinstance(column, `RealValuedColumn`):
        key=column.name, value=a `Tensor`
  """

  def __init__(self,
               feature_columns,
               model_dir=None,
               label_dimension=1,
               weight_feature_key=None,
               optimizer=None,
               config=None,
               partitioner=None):
    """Initializes a `LinearRegressor` instance.

    Args:
      feature_columns: An iterable containing all the feature columns used by
        the model. All items in the set should be instances of classes derived
        from `FeatureColumn`.
      model_dir: Directory to save model parameters, graph and etc. This can
        also be used to load checkpoints from the directory into a estimator
        to continue training a previously saved model.
      label_dimension: Number of regression targets per example. This is the
        size of the last dimension of the labels and logits `Tensor` objects
        (typically, these have shape `[batch_size, label_dimension]`).
      weight_feature_key: A string defining feature column name representing
        weights. It is used to down weight or boost examples during training. It
        will be multiplied by the loss of the example.
      optimizer: string, `tf.Optimizer` object, or callable that returns
        `tf.Optimizer`. Defines the optimizer to use for training. If `None`,
        will use the FTRL optimizer.
      config: `RunConfig` object to configure the runtime settings.
      partitioner: Optional. Partitioner for input layer.
    """
    super(LinearRegressor, self).__init__(
        model_fn=_linear_model_fn,
        model_dir=model_dir,
        config=config,
        params={
            # pylint: disable=protected-access
            'head': head_lib._regression_head_with_mean_squared_error_loss(
                label_dimension=label_dimension,
                weight_feature_key=weight_feature_key),
            # pylint: enable=protected-access
            'feature_columns': feature_columns,
            'optimizer': optimizer,
            'partitioner': partitioner,
        })