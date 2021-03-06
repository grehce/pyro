from __future__ import absolute_import, division, print_function

import numbers

import torch
import math
import numpy as np


def _compute_chain_variance_stats(input):
    # compute within-chain variance and variance estimator
    # input has shape N x C x sample_shape
    N = input.size(0)
    chain_var = input.var(dim=0)
    var_within = chain_var.mean(dim=0)
    var_estimator = (N - 1) / N * var_within
    if input.size(1) > 1:
        chain_mean = input.mean(dim=0)
        var_between = chain_mean.var(dim=0)
        var_estimator = var_estimator + var_between
    return var_within, var_estimator


def gelman_rubin(input, chain_dim=0, sample_dim=1):
    """
    Computes R-hat over chains of samples. It is required that
    ``input.size(sample_dim) >= 2`` and ``input.size(chain_dim) >= 2``.

    :param torch.Tensor input: the input tensor.
    :param int chain_dim: the chain dimension.
    :param int sample_dim: the sample dimension.
    :returns torch.Tensor: R-hat of ``input``.
    """
    assert input.dim() >= 2
    assert input.size(sample_dim) >= 2
    assert input.size(chain_dim) >= 2
    # change input.shape to 1 x 1 x input.shape
    # then transpose sample_dim with 0, chain_dim with 1
    sample_dim = input.dim() + sample_dim if sample_dim < 0 else sample_dim
    chain_dim = input.dim() + chain_dim if chain_dim < 0 else chain_dim
    assert chain_dim != sample_dim
    input = input.reshape((1, 1) + input.shape)
    input = input.transpose(0, sample_dim + 2).transpose(1, chain_dim + 2)

    var_within, var_estimator = _compute_chain_variance_stats(input)
    rhat = (var_estimator / var_within).sqrt()
    return rhat.squeeze(max(sample_dim, chain_dim)).squeeze(min(sample_dim, chain_dim))


def split_gelman_rubin(input, chain_dim=0, sample_dim=1):
    """
    Computes R-hat over chains of samples. It is required that
    ``input.size(sample_dim) >= 4``.

    :param torch.Tensor input: the input tensor.
    :param int chain_dim: the chain dimension.
    :param int sample_dim: the sample dimension.
    :returns torch.Tensor: split R-hat of ``input``.
    """
    assert input.dim() >= 2
    assert input.size(sample_dim) >= 4
    # change input.shape to 1 x 1 x input.shape
    # then transpose chain_dim with 0, sample_dim with 1
    sample_dim = input.dim() + sample_dim if sample_dim < 0 else sample_dim
    chain_dim = input.dim() + chain_dim if chain_dim < 0 else chain_dim
    assert chain_dim != sample_dim
    input = input.reshape((1, 1) + input.shape)
    input = input.transpose(0, chain_dim + 2).transpose(1, sample_dim + 2)

    N_half = input.size(1) // 2
    new_input = torch.stack([input[:, :N_half], input[:, -N_half:]], dim=1)
    new_input = new_input.reshape((-1, N_half) + input.shape[2:])
    split_rhat = gelman_rubin(new_input)
    return split_rhat.squeeze(max(sample_dim, chain_dim)).squeeze(min(sample_dim, chain_dim))


def _fft_next_good_size(N):
    # find the smallest number >= N such that the only divisors are 2, 3, 5
    if N <= 2:
        return 2
    while True:
        m = N
        while m % 2 == 0:
            m //= 2
        while m % 3 == 0:
            m //= 3
        while m % 5 == 0:
            m //= 5
        if m == 1:
            return N
        N += 1


def autocorrelation(input, dim=0):
    """
    Computes the autocorrelation of samples at dimension ``dim``.

    Reference: https://en.wikipedia.org/wiki/Autocorrelation#Efficient_computation

    :param torch.Tensor input: the input tensor.
    :param int dim: the dimension to calculate autocorrelation.
    :returns torch.Tensor: autocorrelation of ``input``.
    """
    if (not input.is_cuda) and (not torch.backends.mkl.is_available()):
        raise NotImplementedError("For CPU tensor, this method is only supported "
                                  "with MKL installed.")

    # Adapted from Stan implementation
    # https://github.com/stan-dev/math/blob/develop/stan/math/prim/mat/fun/autocorrelation.hpp
    N = input.size(dim)
    M = _fft_next_good_size(N)
    M2 = 2 * M

    # transpose dim with -1 for Fourier transform
    input = input.transpose(dim, -1)

    # centering and padding x
    centered_signal = input - input.mean(dim=-1, keepdim=True)
    pad = input.new_zeros(input.shape[:-1] + (M2 - N,))
    centered_signal = torch.cat([centered_signal, pad], dim=-1)

    # Fourier transform
    freqvec = torch.rfft(centered_signal, signal_ndim=1, onesided=False)
    # take square of magnitude of freqvec (or freqvec x freqvec*)
    freqvec_gram = freqvec.pow(2).sum(-1, keepdim=True)
    freqvec_gram = torch.cat([freqvec_gram, input.new_zeros(freqvec_gram.shape)], dim=-1)
    # inverse Fourier transform
    autocorr = torch.irfft(freqvec_gram, signal_ndim=1, onesided=False)

    # truncate and normalize the result, then transpose back to original shape
    autocorr = autocorr[..., :N]
    autocorr = autocorr / input.new_tensor(range(N, 0, -1))
    autocorr = autocorr / autocorr[..., :1]
    return autocorr.transpose(dim, -1)


def autocovariance(input, dim=0):
    """
    Computes the autocovariance of samples at dimension ``dim``.

    :param torch.Tensor input: the input tensor.
    :param int dim: the dimension to calculate autocorrelation.
    :returns torch.Tensor: autocorrelation of ``input``.
    """
    return autocorrelation(input, dim) * input.var(dim, unbiased=False, keepdim=True)


def _cummin(input):
    """
    Computes cummulative minimum of input at dimension ``dim=0``.

    :param torch.Tensor input: the input tensor.
    :returns torch.Tensor: accumulate min of `input` at dimension `dim=0`.
    """
    # FIXME: is there a better trick to find accumulate min of a sequence?
    N = input.size(0)
    input_tril = input.unsqueeze(0).repeat((N,) + (1,) * input.dim())
    triu_mask = input.new_ones(N, N).triu(diagonal=1).reshape((N, N) + (1,) * (input.dim() - 1))
    triu_mask = triu_mask.expand((N, N) + input.shape[1:]) > 0.5
    input_tril.masked_fill_(triu_mask, input.max())
    return input_tril.min(dim=1)[0]


def effective_sample_size(input, chain_dim=0, sample_dim=1):
    """
    Computes effective sample size of input.

    Reference:

    [1] `Introduction to Markov Chain Monte Carlo`,
        Charles J. Geyer

    [2] `Stan Reference Manual version 2.18`,
        Stan Development Team

    :param torch.Tensor input: the input tensor.
    :param int chain_dim: the chain dimension.
    :param int sample_dim: the sample dimension.
    :returns torch.Tensor: effective sample size of ``input``.
    """
    assert input.dim() >= 2
    assert input.size(sample_dim) >= 2
    # change input.shape to 1 x 1 x input.shape
    # then transpose sample_dim with 0, chain_dim with 1
    sample_dim = input.dim() + sample_dim if sample_dim < 0 else sample_dim
    chain_dim = input.dim() + chain_dim if chain_dim < 0 else chain_dim
    assert chain_dim != sample_dim
    input = input.reshape((1, 1) + input.shape)
    input = input.transpose(0, sample_dim + 2).transpose(1, chain_dim + 2)

    N, C = input.size(0), input.size(1)
    # find autocovariance for each chain at lag k
    gamma_k_c = autocovariance(input, dim=0)  # N x C x sample_shape

    # find autocorrelation at lag k (from Stan reference)
    var_within, var_estimator = _compute_chain_variance_stats(input)
    rho_k = (var_estimator - var_within + gamma_k_c.mean(dim=1)) / var_estimator
    rho_k[0] = 1  # correlation at lag 0 is always 1

    # initial positive sequence (formula 1.18 in [1]) applied for autocorrelation
    Rho_k = rho_k if N % 2 == 0 else rho_k[:-1]
    Rho_k = Rho_k.reshape((N // 2, 2) + Rho_k.shape[1:]).sum(dim=1)

    # separate the first index
    Rho_init = Rho_k[0]

    if Rho_k.size(0) > 1:
        # Theoretically, Rho_k is positive, but due to noise of correlation computation,
        # Rho_k might not be positive at some point. So we need to truncate (ignore first index).
        Rho_positive = Rho_k[1:].clamp(min=0)

        # Now we make the initial monotone (decreasing) sequence.
        Rho_monotone = _cummin(Rho_positive)

        # Formula 1.19 in [1]
        tau = -1 + 2 * Rho_init + 2 * Rho_monotone.sum(dim=0)
    else:
        tau = -1 + 2 * Rho_init

    n_eff = C * N / tau
    return n_eff.squeeze(max(sample_dim, chain_dim)).squeeze(min(sample_dim, chain_dim))


def resample(input, num_samples, dim=0, replacement=False):
    """
    Draws ``num_samples`` samples from ``input`` at dimension ``dim``.

    :param torch.Tensor input: the input tensor.
    :param int num_samples: the number of samples to draw from ``input``.
    :param int dim: dimension to draw from ``input``.
    :returns torch.Tensor: samples drawn randomly from ``input``.
    """
    weights = input.new_ones(input.size(dim))
    indices = torch.multinomial(weights, num_samples, replacement)
    return input.index_select(dim, indices)


def quantile(input, probs, dim=0):
    """
    Computes quantiles of ``input`` at ``probs``. If ``probs`` is a scalar,
    the output will be squeezed at ``dim``.

    :param torch.Tensor input: the input tensor.
    :param list probs: quantile positions.
    :param int dim: dimension to take quantiles from ``input``.
    :returns torch.Tensor: quantiles of ``input`` at ``probs``.
    """
    if isinstance(probs, (numbers.Number, list, tuple)):
        probs = input.new_tensor(probs)
    sorted_input = input.sort(dim)[0]
    max_index = input.size(dim) - 1
    indices = probs * max_index
    # because indices is float, we interpolate the quantiles linearly from nearby points
    indices_below = indices.long()
    indices_above = (indices_below + 1).clamp(max=max_index)
    quantiles_above = sorted_input.index_select(dim, indices_above)
    quantiles_below = sorted_input.index_select(dim, indices_below)
    shape_to_broadcast = [1] * input.dim()
    shape_to_broadcast[dim] = indices.numel()
    weights_above = indices - indices_below.type_as(indices)
    weights_above = weights_above.reshape(shape_to_broadcast)
    weights_below = 1 - weights_above
    quantiles = weights_below * quantiles_below + weights_above * quantiles_above
    return quantiles if probs.shape != torch.Size([]) else quantiles.squeeze(dim)


def pi(input, prob, dim=0):
    """
    Computes percentile interval which assigns equal probability mass
    to each tail of the interval.

    :param torch.Tensor input: the input tensor.
    :param float prob: the probability mass of samples within the interval.
    :param int dim: dimension to calculate percentile interval from ``input``.
    :returns torch.Tensor: quantiles of ``input`` at ``probs``.
    """
    return quantile(input, [(1 - prob) / 2, (1 + prob) / 2], dim)


def hpdi(input, prob, dim=0):
    """
    Computes "highest posterior density interval" which is the narrowest
    interval with probability mass ``prob``.

    :param torch.Tensor input: the input tensor.
    :param float prob: the probability mass of samples within the interval.
    :param int dim: dimension to calculate percentile interval from ``input``.
    :returns torch.Tensor: quantiles of ``input`` at ``probs``.
    """
    sorted_input = input.sort(dim)[0]
    mass = input.size(dim)
    index_length = int(prob * mass)
    intervals_left = sorted_input.index_select(
        dim, input.new_tensor(range(mass - index_length), dtype=torch.long))
    intervals_right = sorted_input.index_select(
        dim, input.new_tensor(range(index_length, mass), dtype=torch.long))
    intervals_length = intervals_right - intervals_left
    index_start = intervals_length.argmin(dim)
    indices = torch.stack([index_start, index_start + index_length], dim)
    return torch.gather(sorted_input, dim, indices)


def _weighted_mean(input, log_weights, dim=0, keepdim=False):
    dim = input.dim() + dim if dim < 0 else dim
    log_weights = log_weights.reshape([-1] + (input.dim() - dim - 1) * [1])
    max_log_weight = log_weights.max(dim=0)[0]
    relative_probs = (log_weights - max_log_weight).exp()
    return (input * relative_probs).sum(dim=dim, keepdim=keepdim) / relative_probs.sum()


def _weighted_variance(input, log_weights, dim=0, keepdim=False, unbiased=True):
    # Ref: https://en.wikipedia.org/wiki/Weighted_arithmetic_mean#Frequency_weights
    deviation_squared = (input - _weighted_mean(input, log_weights, dim, keepdim=True)).pow(2)
    correction = log_weights.size(0) / (log_weights.size(0) - 1.) if unbiased else 1.
    return _weighted_mean(deviation_squared, log_weights, dim, keepdim) * correction


def waic(input, log_weights=None, pointwise=False, dim=0):
    """
    Computes "Widely Applicable/Watanabe-Akaike Information Criterion" (WAIC) and
    its corresponding effective number of parameters.

    Reference:

    [1] `WAIC and cross-validation in Stan`,
    Aki Vehtari, Andrew Gelman

    :param torch.Tensor input: the input tensor, which is log likelihood of a model.
    :param torch.Tensor log_weights: weights of samples along ``dim``.
    :param int dim: the sample dimension of ``input``.
    :returns tuple: tuple of WAIC and effective number of parameters.
    """
    log_weights = input.new_zeros(input.size(dim)) if log_weights is None else log_weights

    # computes log pointwise predictive density: formula (3) of [1]
    dim = input.dim() + dim if dim < 0 else dim
    weighted_input = input + log_weights.reshape([-1] + (input.dim() - dim - 1) * [1])
    lpd = torch.logsumexp(weighted_input, dim=dim) - torch.logsumexp(log_weights, dim=0)

    # computes the effective number of parameters: formula (6) of [1]
    p_waic = _weighted_variance(input, log_weights, dim)

    # computes expected log pointwise predictive density: formula (4) of [1]
    elpd = lpd - p_waic
    waic = -2 * elpd
    return (waic, p_waic) if pointwise else (waic.sum(), p_waic.sum())


def fit_generalized_pareto(X):
    """
    Given a dataset X assumed to be drawn from the Generalized Pareto
    Distribution, estimate the distributional parameters k, sigma using a
    variant of the technique described in reference [1]. This
    implementation is modified from the implementation used in reference [2].

    References
    [1] 'A new and efficient estimation method for the generalized Pareto distribution.'
    Zhang, J. and Stephens, M.A. (2009).
    [2] 'Pareto Smoothed Importance Sampling.'
    Aki Vehtari, Andrew Gelman, Jonah Gabry
    https://github.com/avehtari/PSIS/blob/904146236767182270c9718a7b2a30831fe701fe/py/psis.py#L211

    :param torch.Tensor or numpy.ndarray X: the input data X
    :returns tuple: tuple of floats (k, sigma) corresponding to the fit parameters
    """
    if isinstance(X, torch.Tensor):
        X = X.data.cpu().float().numpy()

    if X.ndim != 1 or len(X) <= 1:
        raise ValueError("Invalid input array.")

    X.sort()

    N = len(X)
    PRIOR = 3
    M = 30 + int(math.sqrt(N))

    bs = np.arange(1, M + 1, dtype=float) - 0.5
    bs = 1.0 - math.sqrt(M) / np.sqrt(bs)
    bs /= PRIOR * X[int(N/4 - 0.5)]
    bs += 1 / X[-1]

    temp = np.log1p(-bs[:, None] * X)
    ks = np.mean(temp, axis=1)

    L = np.log(-bs / ks) - (ks + 1)
    L *= N

    temp = np.exp(L - L[:, None])
    w = 1.0 / np.sum(temp, axis=1)

    # remove negligible weights
    dii = w >= 10 * np.finfo(float).eps
    if not np.all(dii):
        w = w[dii]
        bs = bs[dii]
    w /= w.sum()

    b = np.sum(bs * w)
    temp = np.log1p((-b) * X)
    k = np.mean(temp)
    sigma = -k / b
    a = 10  # weakly informative prior for k
    k = k * N / (N + a) + a * 0.5 / (N + a)

    return k, sigma
