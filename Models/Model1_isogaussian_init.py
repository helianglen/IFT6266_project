"""
Convolutional network example.
The original version of this code come from LeNetConvNet.py
https://github.com/mila-udem/blocks-examples/blob/master/mnist_lenet/
"""

import logging
import numpy
from argparse import ArgumentParser

from theano import tensor

from blocks.algorithms import GradientDescent, Scale, Adam, Momentum
from blocks.bricks import (MLP, BatchNormalization, Rectifier,
Initializable, FeedforwardSequence,Softmax, Activation)
from blocks.bricks.conv import (Convolutional, ConvolutionalSequence, Flattener, MaxPooling)
from blocks.bricks.cost import CategoricalCrossEntropy, MisclassificationRate
from blocks.extensions import FinishAfter, Timing, Printing, ProgressBar
from blocks.extensions.monitoring import (DataStreamMonitoring,TrainingDataMonitoring)
from blocks.extensions.saveload import Checkpoint
from blocks.graph import (ComputationGraph, apply_batch_normalization,
get_batch_normalization_updates, apply_dropout, apply_noise)
from blocks.filter import get_brick, VariableFilter
from blocks.roles import add_role, PARAMETER, WEIGHT, FILTER, INPUT, OUTPUT, DROPOUT
from blocks.initialization import Constant, Uniform, Orthogonal, IsotropicGaussian
from blocks.main_loop import MainLoop
from blocks.model import Model
from blocks.monitoring import aggregation
from toolz.itertoolz import interleave
from fuel_transformers_image import MaximumImageDimensions, RandomHorizontalSwap
from plot import Plot
from RRELU_ELU import Rand_Leaky_Rectifier, ELU
#from ScikitResize import ScikitResize
import socket
import datetime

logger = logging.getLogger(__name__)

class LeNet(FeedforwardSequence, Initializable):
    """LeNet-like convolutional network.

    The class implements LeNet, which is a convolutional sequence with
    an MLP on top (several fully-connected layers). For details see
    [LeCun95]_.

    .. [LeCun95] LeCun, Yann, et al.
       *Comparison of learning algorithms for handwritten digit
       recognition.*,
       International conference on artificial neural networks. Vol. 60.

    Parameters
    ----------
    conv_activations : list of :class:`.Brick`
        Activations for convolutional network.
    num_channels : int
        Number of channels in the input image.
    image_shape : tuple
        Input image shape.
    filter_sizes : list of tuples
        Filter sizes of :class:`.blocks.conv.ConvolutionalLayer`.
    feature_maps : list
        Number of filters for each of convolutions.
    pooling_sizes : list of tuples
        Sizes of max pooling for each convolutional layer.
    top_mlp_activations : list of :class:`.blocks.bricks.Activation`
        List of activations for the top MLP.
    top_mlp_dims : list
        Numbers of hidden units and the output dimension of the top MLP.
    conv_step : tuples
        Step of convolution (similar for all layers).
    border_mode : str
        Border mode of convolution (similar for all layers).

    """

    def __init__(self, conv_activations, num_channels, image_shape,
                 filter_sizes, feature_maps, pooling_sizes,
                 top_mlp_activations, top_mlp_dims,
                 conv_step=None, border_mode='valid', **kwargs):
        if conv_step is None:
            self.conv_step = (1, 1)
        else:
            self.conv_step = conv_step
        self.num_channels = num_channels
        self.image_shape = image_shape
        self.top_mlp_activations = top_mlp_activations
        self.top_mlp_dims = top_mlp_dims
        self.border_mode = border_mode

        conv_parameters = zip(filter_sizes, feature_maps)

        # Construct convolutional, activation, and pooling layers with corresponding parameters
        self.convolution_layer = (Convolutional(filter_size=filter_size,
                                               num_filters=num_filter,
                                               step=self.conv_step,
                                               border_mode=self.border_mode,
                                               name='conv_{}'.format(i))
                                 for i, (filter_size, num_filter)
                                 in enumerate(conv_parameters))

        self.BN_layer =          (BatchNormalization(name='bn_conv_{}'.format(i))
                                 for i in enumerate(conv_parameters))

        self.pooling_layer =     (MaxPooling(size, name='pool_{}'.format(i))
                                 for i, size in enumerate(pooling_sizes))

        self.layers = list(interleave([
                            self.convolution_layer,
                            self.BN_layer,
                            conv_activations,
                            self.pooling_layer]))

        self.conv_sequence = ConvolutionalSequence(self.layers, num_channels,
                                                   image_size=image_shape)

        # Construct a top MLP
        self.top_mlp = MLP(top_mlp_activations, top_mlp_dims)

        # We need to flatten the output of the last convolutional layer.
        # This brick accepts a tensor of dimension (batch_size, ...) and
        # returns a matrix (batch_size, features)
        self.flattener = Flattener()
        application_methods = [self.conv_sequence.apply, self.flattener.apply,
                               self.top_mlp.apply]
        super(LeNet, self).__init__(application_methods, **kwargs)

    @property
    def output_dim(self):
        return self.top_mlp_dims[-1]

    @output_dim.setter
    def output_dim(self, value):
        self.top_mlp_dims[-1] = value

    def _push_allocation_config(self):
        self.conv_sequence._push_allocation_config()
        conv_out_dim = self.conv_sequence.get_dim('output')

        self.top_mlp.activations = self.top_mlp_activations
        self.top_mlp.dims = [numpy.prod(conv_out_dim)] + self.top_mlp_dims


def main(save_to, num_epochs, feature_maps=None, mlp_hiddens=None,
         conv_sizes=None, pool_sizes=None, batch_size=200,
         num_batches=None):
    if feature_maps is None:
        feature_maps = [32, 32, 64, 64, 128, 128]
    if mlp_hiddens is None:
        mlp_hiddens = [1000]
    if conv_sizes is None:
        conv_sizes = [7, 5, 5, 5, 3, 3]
    if pool_sizes is None:
        pool_sizes = [2, 2, 2, 2, 2, 2]
    image_size = (128, 128)
    batch_size = 64
    output_size = 2
    learningRate = 0.01
    drop_prob = 0.4
    weight_noise = 0.75
    num_epochs = 150
    num_batches = None


    # Use ReLUs everywhere and softmax for the final prediction
    conv_activations = [Rectifier() for _ in feature_maps]
    mlp_activations = [Rectifier() for _ in mlp_hiddens] + [Softmax()]
    convnet = LeNet(conv_activations, 3, image_size,
                    filter_sizes=zip(conv_sizes, conv_sizes),
                    feature_maps=feature_maps,
                    pooling_sizes=zip(pool_sizes, pool_sizes),
                    top_mlp_activations=mlp_activations,
                    top_mlp_dims=mlp_hiddens + [output_size],
                    border_mode='full',
                    weights_init=IsotropicGaussian(std = 0.2, mean = 0),
                    biases_init=IsotropicGaussian(std = 0.09, mean = 0))
    # We push initialization config to set different initialization schemes
    # for convolutional layers.
    convnet.push_initialization_config()
    convnet.layers[0].weights_init = IsotropicGaussian(std = 0.2, mean = 0)
    convnet.layers[1].weights_init = IsotropicGaussian(std = 0.09, mean = 0)
    convnet.top_mlp.linear_transformations[0].weights_init = IsotropicGaussian(std = 0.8, mean = 0)
    convnet.top_mlp.linear_transformations[1].weights_init = IsotropicGaussian(std = 0.11, mean = 0)
    convnet.initialize()
    logging.info("Input dim: {} {} {}".format(
        *convnet.children[0].get_dim('input_')))
    for i, layer in enumerate(convnet.layers):
        if isinstance(layer, Activation):
            logging.info("Layer {} ({})".format(
                i, layer.__class__.__name__))
        else:
            logging.info("Layer {} ({}) dim: {} {} {}".format(
                i, layer.__class__.__name__, *layer.get_dim('output')))
    x = tensor.tensor4('image_features')
    y = tensor.lmatrix('targets')

    # Normalize input and apply the convnet
    probs = convnet.apply(x)
    cost = (CategoricalCrossEntropy().apply(y.flatten(), probs).copy(name='cost'))
    error_rate = (MisclassificationRate().apply(y.flatten(), probs).copy(name='error_rate'))
    error_rate2 = error_rate.copy(name='error_rate2')

    cg = ComputationGraph([cost, error_rate])
    weights = VariableFilter(roles=[FILTER, WEIGHT])(cg.variables)

    ############# Dropout #############

    logger.info('Applying dropout')
    cg = apply_dropout(cg, weights[-1:0], drop_prob) #Dropout only on fully-connected layer
    dropped_out = VariableFilter(roles=[DROPOUT])(cg.variables)

    ############# Guaussian Noise #############

    logger.info('Applying Gaussian noise')
    cg = apply_noise(cg, weights, weight_noise)

    ########### Loading images #####################

    from fuel.datasets.dogs_vs_cats import DogsVsCats
    from fuel.streams import DataStream, ServerDataStream
    from fuel.schemes import ShuffledScheme
    from fuel.transformers.image import RandomFixedSizeCrop, MinimumImageDimensions, Random2DRotation
    from fuel.transformers import Flatten, Cast, ScaleAndShift

    def create_data(data):
        stream = DataStream(data, iteration_scheme=ShuffledScheme(data.num_examples, batch_size))
        stream = MinimumImageDimensions(stream, image_size, which_sources=('image_features',))
        stream = MaximumImageDimensions(stream, image_size, which_sources=('image_features',))
        stream = RandomHorizontalSwap(stream, which_sources=('image_features',))
        stream = Random2DRotation(stream, which_sources=('image_features',))
        #stream = ScikitResize(stream, image_size, which_sources=('image_features',))
        stream = ScaleAndShift(stream, 1./255, 0, which_sources=('image_features',))
        stream = Cast(stream, dtype='float32', which_sources=('image_features',))
        return stream

    #stream_data_train = ServerDataStream(('image_features','targets'), False, port=5560)
    #stream_data_valid = ServerDataStream(('image_features','targets'), False, port=5561)
    stream_data_train = create_data(DogsVsCats(('train',), subset=slice(0, 22500)))
    stream_data_valid = create_data(DogsVsCats(('train',), subset=slice(22500, 25000)))
    #stream_data_train = create_data(DogsVsCats(('train',), subset=slice(0, 10)))
    #stream_data_valid = create_data(DogsVsCats(('train',), subset=slice(10, 12)))

    # Train with simple SGD
    # On the importance of initialization and momentum in deep learning: choose lowest momentum w/ lowest error
    algorithm = GradientDescent(cost=cost, parameters=cg.parameters,step_rule=Momentum(learning_rate=learningRate, momentum=0.995))
    #algorithm = GradientDescent(cost=cost, parameters=cg.parameters,step_rule=Scale(learning_rate=learningRate))
    #algorithm = GradientDescent(cost=cost, parameters=cg.parameters,step_rule=Adam(0.001))


    # `Timing` extension reports time for reading data, aggregating a batch
    # and monitoring;
    # `ProgressBar` displays a nice progress bar during training.
    extensions = []
    extensions.append(Timing())
    extensions.append(FinishAfter(after_n_epochs=num_epochs,after_n_batches=num_batches))
    extensions.append(DataStreamMonitoring([cost, error_rate],stream_data_valid,prefix="valid"))
    extensions.append(TrainingDataMonitoring([cost, error_rate,aggregation.mean(algorithm.total_gradient_norm)],prefix="train",after_epoch=True))
    extensions.append(Checkpoint("Model1_isogaussian_init.pkl", after_epoch=True, after_training=True, save_separately=['log']))
    extensions.append(ProgressBar())
    extensions.append(Printing())

    host_plot='http://hades:5090'
    extensions.append(Plot('%s %s @ %s' % ('CNN1_isogaussian', datetime.datetime.now(), socket.gethostname()),
                        channels=[['train_error_rate', 'valid_error_rate'],
                         ['train_total_gradient_norm']], after_epoch=True, server_url=host_plot))
    logger.info("Building the model")

    model = Model(cost)

    ########### Loading images #####################
    main_loop = MainLoop(
        algorithm,
        stream_data_train,
        model=model,
        extensions=extensions)

    main_loop.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = ArgumentParser("An example of training a convolutional network "
                            "on the CatVsDog dataset.")
    parser.add_argument("--num-epochs", type=int, default=2,
                        help="Number of training epochs to do.")
    parser.add_argument("save_to", default="mnist.pkl", nargs="?",
                        help="Destination to save the state of the training "
                             "process.")
    parser.add_argument("--feature-maps", type=int, nargs='+',
                        default=[32, 32, 64, 64, 128, 128], help="List of feature maps numbers.")
    parser.add_argument("--mlp-hiddens", type=int, nargs='+', default=[20],
                        help="List of numbers of hidden units for the MLP.")
    parser.add_argument("--conv-sizes", type=int, nargs='+', default=[7, 5, 5, 5, 3, 3],
                        help="Convolutional kernels sizes. The kernels are "
                        "always square.")
    parser.add_argument("--pool-sizes", type=int, nargs='+', default=[2, 2, 2, 2, 2, 2],
                        help="Pooling sizes. The pooling windows are always "
                             "square. Should be the same length as "
                             "--conv-sizes.")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="Batch size.")
    args = parser.parse_args()
    main(**vars(args))
