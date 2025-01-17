import logging
from typing import Any, Dict, List, Tuple, Union

import awkward as ak
import numpy as np
import pyhf


log = logging.getLogger(__name__)


def model_and_data(
    spec: Dict[str, Any], asimov: bool = False, with_aux: bool = True
) -> Tuple[pyhf.pdf.Model, List[float]]:
    """Returns model and data for a ``pyhf`` workspace specification.

    Args:
        spec (Dict[str, Any]): a ``pyhf`` workspace specification
        asimov (bool, optional): whether to return the Asimov dataset, defaults to False
        with_aux (bool, optional): whether to also return auxdata, defaults to True

    Returns:
        Tuple[pyhf.pdf.Model, List[float]]:
            - a HistFactory-style model in ``pyhf`` format
            - the data (plus auxdata if requested) for the model
    """
    workspace = pyhf.Workspace(spec)
    model = workspace.model(
        modifier_settings={
            "normsys": {"interpcode": "code4"},
            "histosys": {"interpcode": "code4p"},
        }
    )  # use HistFactory InterpCode=4
    if not asimov:
        data = workspace.data(model, with_aux=with_aux)
    else:
        data = asimov_data(model, with_aux=with_aux)
    return model, data


def parameter_names(model: pyhf.pdf.Model) -> List[str]:
    """Returns the labels of all fit parameters.

    Vectors that act on one bin per vector entry (gammas) are expanded.

    Args:
        model (pyhf.pdf.Model): a HistFactory-style model in ``pyhf`` format

    Returns:
        List[str]: names of fit parameters
    """
    labels = []
    for parname in model.config.par_order:
        for i_par in range(model.config.param_set(parname).n_parameters):
            labels.append(
                f"{parname}[bin_{i_par}]"
                if model.config.param_set(parname).n_parameters > 1
                else parname
            )
    return labels


def asimov_data(model: pyhf.Model, with_aux: bool = True) -> List[float]:
    """Returns the Asimov dataset (optionally with auxdata) for a model.

    Initial parameter settings for normalization factors in the workspace are treated as
    the default settings for that parameter. Fitting the Asimov dataset will recover
    these initial settings as the maximum likelihood estimate for normalization factors.
    Initial settings for other modifiers are ignored.

    Args:
        model (pyhf.Model): the model from which to construct the dataset
        with_aux (bool, optional): whether to also return auxdata, defaults to True

    Returns:
        List[float]: the Asimov dataset
    """
    asimov_data = pyhf.tensorlib.tolist(
        model.expected_data(asimov_parameters(model), include_auxdata=with_aux)
    )
    return asimov_data


def asimov_parameters(model: pyhf.pdf.Model) -> np.ndarray:
    """Returns a list of Asimov parameter values for a model.

    For normfactors and shapefactors, initial parameter settings (specified in the
    workspace) are treated as nominal settings. This ignores custom auxiliary data set
    in the measurement configuration in the workspace.

    Args:
        model (pyhf.pdf.Model): model for which to extract the parameters

    Returns:
        np.ndarray: the Asimov parameters, in the same order as
        ``model.config.suggested_init()``
    """
    # create a list of Asimov parameters (constrained parameters at best-fit value from
    # the aux measurement, unconstrained parameters at init specified in the workspace)
    asimov_parameters = []
    for parameter in model.config.par_order:
        if not model.config.param_set(parameter).constrained:
            # unconstrained parameter: use suggested inits (for normfactor/shapefactor)
            inits = model.config.param_set(parameter).suggested_init
        elif dict(model.config.modifiers)[parameter] in ["histosys", "normsys"]:
            # histosys/normsys: Gaussian constraint, nominal value 0
            inits = [0.0] * model.config.param_set(parameter).n_parameters
        else:
            # remaining modifiers are staterror/lumi with Gaussian constraint, and
            # shapesys with Poisson constraint, all have nominal value of 1
            inits = [1.0] * model.config.param_set(parameter).n_parameters

        asimov_parameters += inits

    return np.asarray(asimov_parameters)


def prefit_uncertainties(model: pyhf.pdf.Model) -> np.ndarray:
    """Returns a list of pre-fit parameter uncertainties for a model.

    For unconstrained parameters the uncertainty is set to 0. It is also set to 0 for
    fixed parameters (similarly to how the post-fit uncertainties are defined to be 0).

    Args:
        model (pyhf.pdf.Model): model for which to extract the parameters

    Returns:
        np.ndarray: pre-fit uncertainties for the parameters, in the same order as
        ``model.config.suggested_init()``
    """
    pre_fit_unc = []  # pre-fit uncertainties for parameters
    for parameter in model.config.par_order:
        # obtain pre-fit uncertainty for constrained, non-fixed parameters
        if (
            model.config.param_set(parameter).constrained
            and not model.config.param_set(parameter).suggested_fixed
        ):
            pre_fit_unc += model.config.param_set(parameter).width()
        else:
            if model.config.param_set(parameter).n_parameters == 1:
                # unconstrained normfactor or fixed parameter, uncertainty is 0
                pre_fit_unc.append(0.0)
            else:
                # shapefactor
                pre_fit_unc += [0.0] * model.config.param_set(parameter).n_parameters
    return np.asarray(pre_fit_unc)


def _channel_boundary_indices(model: pyhf.pdf.Model) -> List[int]:
    """Returns indices for splitting a concatenated list of observations into channels.

    This is useful in combination with ``pyhf.pdf.Model.expected_data``, which returns
    the yields across all bins in all channels. These indices mark the positions where a
    channel begins. No index is returned for the first channel, which begins at ``[0]``.
    The returned indices can be used with ``numpy.split``.

    Args:
        model (pyhf.pdf.Model): the model that defines the channels

    Returns:
        List[int]: indices of positions where a channel begins, no index is included for
        the first bin of the first channel (which is always at ``[0]``)
    """
    # get the amount of bins per channel
    bins_per_channel = [model.config.channel_nbins[ch] for ch in model.config.channels]
    # indices of positions where a new channel starts (from the second channel onwards)
    channel_start = [sum(bins_per_channel[:i]) for i in range(1, len(bins_per_channel))]
    return channel_start


def yield_stdev(
    model: pyhf.pdf.Model,
    parameters: np.ndarray,
    uncertainty: np.ndarray,
    corr_mat: np.ndarray,
) -> Tuple[List[List[float]], List[float]]:
    """Calculates symmetrized yield standard deviation of a model, per bin and channel.

    Returns both the uncertainties per bin (in a list of channels), and the uncertainty
    of the total yield per channel (again, for a list of channels). To calculate the
    uncertainties for the total yield, the function internally treats the sum of yields
    per channel like another channel with one bin.

    Args:
        model (pyhf.pdf.Model): the model for which to calculate the standard deviations
            for all bins
        parameters (np.ndarray): central values of model parameters
        uncertainty (np.ndarray): uncertainty of model parameters
        corr_mat (np.ndarray): correlation matrix

    Returns:
        Tuple[List[List[float]], List[float]]:
            - list of channels, each channel is a list of standard deviations per bin
            - list of standard deviations per channel
    """
    # indices where to split to separate all bins into regions
    region_split_indices = _channel_boundary_indices(model)

    # the lists up_variations and down_variations will contain the model distributions
    # with all parameters varied individually within uncertainties
    # indices: variation, channel, bin
    # following the channels contained in the model, there are additional entries with
    # yields summed per channel (internally treated like additional channels) to get the
    # per-channel uncertainties
    up_variations = []
    down_variations = []

    # calculate the model distribution for every parameter varied up and down
    # within the respective uncertainties
    for i_par in range(model.config.npars):
        # central parameter values, but one parameter varied within uncertainties
        up_pars = parameters.copy().astype(float)  # ensure float for correct addition
        up_pars[i_par] += uncertainty[i_par]
        down_pars = parameters.copy().astype(float)
        down_pars[i_par] -= uncertainty[i_par]

        # total model distribution with this parameter varied up
        up_combined = pyhf.tensorlib.to_numpy(
            model.expected_data(up_pars, include_auxdata=False)
        )
        up_yields = np.split(up_combined, region_split_indices)
        # append list of yields summed per channel
        up_yields += [np.asarray([sum(chan_yields)]) for chan_yields in up_yields]
        up_variations.append(up_yields)

        # total model distribution with this parameter varied down
        down_combined = pyhf.tensorlib.to_numpy(
            model.expected_data(down_pars, include_auxdata=False)
        )
        down_yields = np.split(down_combined, region_split_indices)
        # append list of yields summed per channel
        down_yields += [np.asarray([sum(chan_yields)]) for chan_yields in down_yields]
        down_variations.append(down_yields)

    # convert to awkward arrays for further processing
    up_variations = ak.from_iter(up_variations)
    down_variations = ak.from_iter(down_variations)

    # total variance, indices are: channel, bin
    n_channels = len(model.config.channels)
    total_variance_list = [
        np.zeros(model.config.channel_nbins[ch]) for ch in model.config.channels
    ]  # list of arrays, each array has as many entries as there are bins
    # append placeholders for total yield uncertainty per channel
    total_variance_list += [np.asarray([0]) for _ in range(n_channels)]
    total_variance = ak.from_iter(total_variance_list)

    # loop over parameters to sum up total variance
    # first do the diagonal of the correlation matrix
    for i_par in range(model.config.npars):
        symmetric_uncertainty = (up_variations[i_par] - down_variations[i_par]) / 2
        total_variance = total_variance + symmetric_uncertainty ** 2

    labels = parameter_names(model)
    # continue with off-diagonal contributions if there are any
    if np.count_nonzero(corr_mat - np.diag(np.ones_like(parameters))) > 0:
        # loop over pairs of parameters
        for i_par in range(model.config.npars):
            for j_par in range(model.config.npars):
                if j_par >= i_par:
                    continue  # only loop over the half the matrix due to symmetry
                corr = corr_mat[i_par, j_par]
                # an approximate calculation could be done here by requiring
                # e.g. abs(corr) > 1e-5 to continue
                if (
                    labels[i_par][0:10] == "staterror_"
                    and labels[j_par][0:10] == "staterror_"
                ):
                    continue  # two different staterrors are orthogonal, no contribution
                sym_unc_i = (up_variations[i_par] - down_variations[i_par]) / 2
                sym_unc_j = (up_variations[j_par] - down_variations[j_par]) / 2
                # factor of two below is there since loop is only over half the matrix
                total_variance = total_variance + 2 * (corr * sym_unc_i * sym_unc_j)

    # convert to standard deviations per bin and per channel
    total_stdev_per_bin = np.sqrt(total_variance[:n_channels])
    total_stdev_per_channel = ak.flatten(np.sqrt(total_variance[n_channels:]))
    log.debug(f"total stdev is {total_stdev_per_bin}")
    log.debug(f"total stdev per channel is {total_stdev_per_channel}")
    return ak.to_list(total_stdev_per_bin), ak.to_list(total_stdev_per_channel)


def unconstrained_parameter_count(model: pyhf.pdf.Model) -> int:
    """Returns the number of unconstrained parameters in a model.

    The number is the sum of all independent parameters in a fit. A shapefactor that
    affects multiple bins enters the count once for each independent bin. Parameters
    that are set to constant are not included in the count.

    Args:
        model (pyhf.pdf.Model): model to count parameters for

    Returns:
        int: number of unconstrained parameters
    """
    n_pars = 0
    for parname in model.config.par_order:
        if (
            not model.config.param_set(parname).constrained
            and not model.config.param_set(parname).suggested_fixed
        ):
            n_pars += model.config.param_set(parname).n_parameters
    return n_pars


def _parameter_index(par_name: str, labels: Union[List[str], Tuple[str, ...]]) -> int:
    """Returns the position of a parameter with a given name in the list of parameters.

    Useful together with ``parameter_names`` to find the position of a parameter
    when the name is known. If the parameter is not found, logs an error and returns a
    default value of -1.

    Args:
        par_name (str): name of parameter to find in list
        labels (Union[List[str], Tuple[str, ...]]): list or tuple with all parameter
            names in the model

    Returns:
        int: index of parameter
    """
    par_index = next((i for i, label in enumerate(labels) if label == par_name), -1)
    if par_index == -1:
        log.error(f"parameter {par_name} not found in model")
    return par_index
