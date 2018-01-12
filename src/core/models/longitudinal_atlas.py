import os.path
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + os.path.sep + '../../')

import numpy as np
import math

import torch
from torch.autograd import Variable
import warnings

from pydeformetrica.src.core.models.abstract_statistical_model import AbstractStatisticalModel
from pydeformetrica.src.in_out.deformable_object_reader import DeformableObjectReader
from pydeformetrica.src.in_out.dataset_functions import create_template_metadata, compute_noise_dimension
from pydeformetrica.src.core.model_tools.deformations.spatiotemporal_reference_frame import SpatiotemporalReferenceFrame
from pydeformetrica.src.core.observations.deformable_objects.deformable_multi_object import DeformableMultiObject
from pydeformetrica.src.support.utilities.general_settings import Settings
from pydeformetrica.src.core.models.model_functions import create_regular_grid_of_points, compute_sobolev_gradient
from pydeformetrica.src.support.kernels.kernel_functions import create_kernel
from pydeformetrica.src.in_out.utils import *
from pydeformetrica.src.core.model_tools.attachments.multi_object_attachment import MultiObjectAttachment
from pydeformetrica.src.support.probability_distributions.normal_distribution import NormalDistribution
from pydeformetrica.src.support.probability_distributions.inverse_wishart_distribution import InverseWishartDistribution
from pydeformetrica.src.support.probability_distributions.multi_scalar_inverse_wishart_distribution import \
    MultiScalarInverseWishartDistribution
from pydeformetrica.src.support.probability_distributions.multi_scalar_normal_distribution import \
    MultiScalarNormalDistribution


class LongitudinalAtlas(AbstractStatisticalModel):
    """
    Longitudinal atlas object class.
    See "Learning distributions of shape trajectories from longitudinal datasets: a hierarchical model on a manifold
    of diffeomorphisms", Bône et al. (2018), in review.

    """

    ####################################################################################################################
    ### Constructor:
    ####################################################################################################################

    def __init__(self):
        AbstractStatisticalModel.__init__(self)

        self.template = DeformableMultiObject()
        self.objects_name = []
        self.objects_name_extension = []
        self.objects_noise_dimension = []

        self.multi_object_attachment = None
        self.spatiotemporal_reference_frame = SpatiotemporalReferenceFrame()
        self.number_of_sources = None

        self.use_sobolev_gradient = True
        self.smoothing_kernel_width = None

        self.initial_cp_spacing = None
        self.number_of_objects = None
        self.number_of_control_points = None
        self.bounding_box = None

        # Dictionary of numpy arrays.
        self.fixed_effects['template_data'] = None
        self.fixed_effects['control_points'] = None
        self.fixed_effects['momenta'] = None
        self.fixed_effects['modulation_matrix'] = None
        self.fixed_effects['reference_time'] = None
        self.fixed_effects['time_shift_variance'] = None
        self.fixed_effects['log_acceleration_variance'] = None
        self.fixed_effects['noise_variance'] = None

        # Dictionary of probability distributions.
        self.priors['template_data'] = MultiScalarNormalDistribution()
        self.priors['control_points'] = MultiScalarNormalDistribution()
        self.priors['momenta'] = MultiScalarNormalDistribution()
        self.priors['modulation_matrix'] = MultiScalarNormalDistribution()
        self.priors['reference_time'] = MultiScalarNormalDistribution()
        self.priors['time_shift_variance'] = MultiScalarInverseWishartDistribution()
        self.priors['log_acceleration_variance'] = MultiScalarInverseWishartDistribution()
        self.priors['noise_variance'] = MultiScalarInverseWishartDistribution()

        # Dictionary of probability distributions.
        self.individual_random_effects['sources'] = MultiScalarNormalDistribution()
        self.individual_random_effects['onset_age'] = MultiScalarNormalDistribution()
        self.individual_random_effects['log_acceleration'] = MultiScalarNormalDistribution()

        self.freeze_template = False
        self.freeze_control_points = False

    ####################################################################################################################
    ### Encapsulation methods:
    ####################################################################################################################

    # Template data ----------------------------------------------------------------------------------------------------
    def get_template_data(self):
        return self.fixed_effects['template_data']

    def set_template_data(self, td):
        self.fixed_effects['template_data'] = td
        self.template.set_data(td)

    # Control points ---------------------------------------------------------------------------------------------------
    def get_control_points(self):
        return self.fixed_effects['control_points']

    def set_control_points(self, cp):
        self.fixed_effects['control_points'] = cp
        self.number_of_control_points = len(cp)

    # Momenta ----------------------------------------------------------------------------------------------------------
    def get_momenta(self):
        return self.fixed_effects['momenta']

    def set_momenta(self, mom):
        self.fixed_effects['momenta'] = mom

    # Modulation matrix ------------------------------------------------------------------------------------------------
    def get_modulation_matrix(self):
        return self.fixed_effects['modulation_matrix']

    def set_modulation_matrix(self, mm):
        self.fixed_effects['modulation_matrix'] = mm

    # Reference time ---------------------------------------------------------------------------------------------------
    def get_reference_time(self):
        return self.fixed_effects['reference_time']

    def set_reference_time(self, rt):
        self.fixed_effects['reference_time'] = rt
        self.individual_random_effects['onset_age'].mean = np.zeros((1,)) + rt

    # Time-shift variance ----------------------------------------------------------------------------------------------
    def get_time_shift_variance(self):
        return self.fixed_effects['time_shift_variance']

    def set_time_shift_variance(self, tsv):
        self.fixed_effects['time_shift_variance'] = tsv
        self.individual_random_effects['onset_age'].set_variance(tsv)

    # Log-acceleration variance ----------------------------------------------------------------------------------------
    def get_log_acceleration_variance(self):
        return self.fixed_effects['log_acceleration_variance']

    def set_log_acceleration_variance(self, lav):
        self.fixed_effects['log_acceleration_variance'] = lav
        self.individual_random_effects['log_acceleration'].set_variance(lav)

    # Noise variance ---------------------------------------------------------------------------------------------------
    def get_noise_variance(self):
        return self.fixed_effects['noise_variance']

    def set_noise_variance(self, nv):
        self.fixed_effects['noise_variance'] = nv

    # Class 2 fixed effects --------------------------------------------------------------------------------------------
    def get_fixed_effects(self):
        out = {}
        if not self.freeze_template: out['template_data'] = self.fixed_effects['template_data']
        if not self.freeze_control_points: out['control_points'] = self.fixed_effects['control_points']
        out['momenta'] = self.fixed_effects['momenta']
        out['modulation_matrix'] = self.fixed_effects['modulation_matrix']
        return out

    def set_fixed_effects(self, fixed_effects):
        if not self.freeze_template: self.set_template_data(fixed_effects['template_data'])
        if not self.freeze_control_points: self.set_control_points(fixed_effects['control_points'])
        self.set_momenta(fixed_effects['momenta'])
        self.set_modulation_matrix(fixed_effects['modulation_matrix'])

    ####################################################################################################################
    ### Public methods:
    ####################################################################################################################

    def update(self):
        """
        Final initialization steps.
        """
        self._initialize_bounding_box()
        self._initialize_source_variables()
        self._initialize_time_shift_variables()
        self._initialize_log_acceleration_variables()
        self._initialize_noise_variables()

    def compute_log_likelihood(self, dataset, population_RER, individual_RER, with_grad=False):
        """
        Compute the log-likelihood of the dataset, given parameters fixed_effects and random effects realizations
        population_RER and indRER.
        Start by updating the class 1 fixed effects.

        :param dataset: LongitudinalDataset instance
        :param population_RER: Dictionary of population random effects realizations.
        :param individual_RER: Dictionary of individual random effects realizations.
        :param with_grad: Flag that indicates wether the gradient should be returned as well.
        :return:
        """

        # Initialize: conversion from numpy to torch -------------------------------------------------------------------
        template_data, control_points, momenta, modulation_matrix = self._fixed_effects_to_torch_tensors(with_grad)
        sources, onset_ages, log_accelerations = self._individual_RER_to_torch_tensors(individual_RER, with_grad)

        # Deform, update, compute metrics ------------------------------------------------------------------------------
        residuals = self._compute_residuals(dataset, template_data, control_points, momenta, modulation_matrix,
                                            sources, onset_ages, log_accelerations)
        sufficient_statistics = self.compute_sufficient_statistics(dataset, population_RER, individual_RER,
                                                                   residuals=residuals)
        self.update_fixed_effects(dataset, sufficient_statistics)
        attachment = self._compute_attachment(residuals)
        regularity = self._compute_random_effects_regularity(sources, onset_ages, log_accelerations)
        regularity += self._compute_class1_priors_regularity()
        regularity += self._compute_class2_priors_regularity(template_data, control_points, momenta, modulation_matrix)

        # Compute gradient if needed -----------------------------------------------------------------------------------
        if with_grad:
            total = attachment + regularity
            total.backward()

            gradient = {}
            # Template data.
            if not self.freeze_template:
                if self.use_sobolev_gradient:
                    gradient['template_data'] = compute_sobolev_gradient(
                        template_data.grad, self.smoothing_kernel_width, self.template).data.numpy()
                else:
                    gradient['template_data'] = template_data.grad.data.numpy()
            # Other gradients.
            if not self.freeze_control_points: gradient['control_points'] = control_points.grad.data.numpy()
            gradient['momenta'] = momenta.grad.data.cpu().numpy()
            gradient['modulation_matrix'] = modulation_matrix.grad.data.cpu().numpy()
            gradient['sources'] = sources.grad.data.cpu().numpy()
            gradient['onset_age'] = onset_ages.grad.data.cpu().numpy()
            gradient['log_acceleration'] = log_accelerations.grad.data.cpu().numpy()

            return attachment.data.cpu().numpy()[0], regularity.data.cpu().numpy()[0], gradient

        else:
            return attachment.data.cpu().numpy()[0], regularity.data.cpu().numpy()[0]

    def compute_model_log_likelihood(self, dataset, fixed_effects, population_RER, individual_RER, with_grad=False):
        """
        Computes the model log-likelihood, i.e. only the attachment part.
        Returns a list of terms, each element corresponding to a subject.
        Optionally returns the gradient with respect to the non-frozen fixed effects.
        """

        # Initialize: conversion from numpy to torch -------------------------------------------------------------------
        # Template data.
        if not self.freeze_template:
            template_data = fixed_effects['template_data']
            template_data = Variable(torch.from_numpy(template_data).type(Settings().tensor_scalar_type),
                                     requires_grad=with_grad)
        else:
            template_data = self.fixed_effects['template_data']
            template_data = Variable(torch.from_numpy(template_data).type(Settings().tensor_scalar_type),
                                     requires_grad=False)

        # Control points.
        if not self.freeze_control_points:
            control_points = fixed_effects['control_points']
            control_points = Variable(torch.from_numpy(control_points).type(Settings().tensor_scalar_type),
                                      requires_grad=with_grad)
        else:
            control_points = self.fixed_effects['control_points']
            control_points = Variable(torch.from_numpy(control_points).type(Settings().tensor_scalar_type),
                                      requires_grad=False)

        # Momenta.
        momenta = individual_RER['momenta']
        momenta = Variable(torch.from_numpy(momenta).type(Settings().tensor_scalar_type), requires_grad=False)

        # Compute residual, and then the attachment term ---------------------------------------------------------------
        residuals = self._compute_residuals(dataset, template_data, control_points, momenta)
        attachments = self._compute_individual_attachments(residuals)

        # Compute gradients if required --------------------------------------------------------------------------------
        if with_grad:
            attachment = torch.sum(attachments)
            attachment.backward()

            gradient = {}
            # Template data.
            if not self.freeze_template:
                if self.use_sobolev_gradient:
                    gradient['template_data'] = compute_sobolev_gradient(
                        template_data.grad, self.smoothing_kernel_width, self.template).data.numpy()
                else:
                    gradient['template_data'] = template_data.grad.data.numpy()

            # Control points.
            if not self.freeze_control_points: gradient['control_points'] = control_points.grad.data.numpy()

            return attachments.data.cpu().numpy(), gradient

        else:
            return attachments.data.cpu().numpy()

    def compute_sufficient_statistics(self, dataset, population_RER, individual_RER, residuals=None):
        """
        Compute the model sufficient statistics.
        """
        if residuals is None:
            # Initialize: conversion from numpy to torch ---------------------------------------------------------------
            template_data, control_points, momenta, modulation_matrix = self._fixed_effects_to_torch_tensors(False)
            sources, onset_ages, log_accelerations = self._individual_RER_to_torch_tensors(individual_RER, False)

            # Compute residuals ----------------------------------------------------------------------------------------
            residuals = self._compute_residuals(dataset, template_data, control_points, momenta, modulation_matrix,
                                                sources, onset_ages, log_accelerations)

        # Compute sufficient statistics --------------------------------------------------------------------------------
        sufficient_statistics = {}

        # First statistical moment of the onset ages.
        onset_ages = individual_RER['onset_age']
        sufficient_statistics['S1'] = np.mean(onset_ages)

        # Second statistical moment of the onset ages.
        sufficient_statistics['S2'] = np.sum(onset_ages ** 2)

        # Second statistical moment of the log accelerations.
        log_accelerations = individual_RER['log_acceleration']
        sufficient_statistics['S3'] = np.sum(log_accelerations ** 2)

        # Second statistical moment of the residuals.
        sufficient_statistics['S4'] = np.zeros((self.number_of_objects,))
        for i in range(len(residuals)):
            for j in range(len(residuals[i])):
                for k in range(self.number_of_objects):
                    sufficient_statistics['S4'][k] += residuals[i][j][k].data.numpy()[0]

        return sufficient_statistics

    def update_fixed_effects(self, dataset, sufficient_statistics):
        """
        Updates the fixed effects based on the sufficient statistics, maximizing the likelihood.
        """
        number_of_subjects = dataset.number_of_subjects

        # Intricate update of the reference time and the time-shift variance -------------------------------------------
        reftime_prior_mean = self.priors['reference_time'].mean[0]
        reftime_prior_variance = self.priors['reference_time'].variance_sqrt ** 2
        tshiftvar_prior_scale = self.priors['time_shift_variance'].scale_scalars[0]
        tshiftvar_prior_dof = self.priors['time_shift_variance'].degrees_of_freedom[0]

        reftime_old, reftime_new = self.get_reference_time(), self.get_reference_time()
        tshiftvar_old, tshiftvar_new = self.get_time_shift_variance(), self.get_time_shift_variance()

        max_number_of_iterations = 100
        convergence_tolerance = 1e-5
        maximum_difference = 0.0

        for iteration in range(max_number_of_iterations):
            reftime_new = (reftime_prior_variance * sufficient_statistics['S1'] + tshiftvar_new * reftime_prior_mean) \
                          / (number_of_subjects * reftime_prior_variance + tshiftvar_new)
            tshiftvar_new = (sufficient_statistics['S2'] - 2 * reftime_new * sufficient_statistics['S1']
                             + number_of_subjects * reftime_new ** 2 + tshiftvar_prior_dof * tshiftvar_prior_scale) \
                            / (number_of_subjects + tshiftvar_prior_scale)

            maximum_difference = max(math.fabs(reftime_new - reftime_old), math.fabs(tshiftvar_new - tshiftvar_old))
            if maximum_difference < convergence_tolerance:
                break
            else:
                reftime_old = reftime_new
                tshiftvar_old = tshiftvar_new

        if iteration == max_number_of_iterations:
            msg = 'In longitudinal_atlas.update_fixed_effects, the intricate update of the reference time and ' \
                  'time-shift variance does not satisfy the tolerance threshold. Maximum difference = ' \
                  + str(maximum_difference) + ' > tolerance = ' + str(convergence_tolerance)
            warnings.warn(msg)

        self.set_reference_time(reftime_new)
        self.set_time_shift_variance(tshiftvar_new)

        # Update of the log-acceleration variance ----------------------------------------------------------------------
        prior_scale = self.priors['log_acceleration_variance'].scale_scalars[0]
        prior_dof = self.priors['log_acceleration_variance'].degrees_of_freedom[0]
        log_acceleration_variance = (sufficient_statistics["S3"] + prior_dof * prior_scale) \
                                    / (number_of_subjects + prior_dof)
        self.set_log_acceleration_variance(log_acceleration_variance)

        # Update of the residual noise variance ------------------------------------------------------------------------
        noise_variance = np.zeros((self.number_of_objects,))
        prior_scale_scalars = self.priors['noise_variance'].scale_scalars
        prior_dofs = self.priors['noise_variance'].degrees_of_freedom
        for k in range(self.number_of_objects):
            noise_variance[k] = (sufficient_statistics['S4'] + prior_scale_scalars[k] * prior_dofs[k]) \
                                / (number_of_subjects * self.objects_noise_dimension[k] + prior_dofs[k])
        self.set_noise_variance(noise_variance)

    ####################################################################################################################
    ### Private key methods:
    ####################################################################################################################

    def _compute_attachment(self, residuals):
        """
        Fully torch.
        """
        return torch.sum(self._compute_individual_attachments(residuals))

    def _compute_individual_attachments(self, residuals):
        """
        Fully torch.
        """
        number_of_subjects = len(residuals)
        attachments = Variable(torch.zeros((number_of_subjects,)).type(Settings().tensor_scalar_type),
                               requires_grad=False)
        for i in range(number_of_subjects):
            attachment_i = 0.0
            for j in range(len(residuals[i])):
                attachment_i -= 0.5 * torch.sum(residuals[i][j] / Variable(
                    torch.from_numpy(self.fixed_effects['noise_variance']).type(Settings().tensor_scalar_type),
                    requires_grad=False))
            attachments[i] = attachment_i
        return attachments

    def _compute_random_effects_regularity(self, sources, onset_ages, log_accelerations):
        """
        Fully torch.
        """
        number_of_subjects = onset_ages.shape[0]
        regularity = 0.0

        # Sources random effect.
        for i in range(number_of_subjects):
            regularity += self.individual_random_effects['sources'].compute_log_likelihood_torch(sources[i])

        # Onset age random effect.
        for i in range(number_of_subjects):
            regularity += self.individual_random_effects['onset_age'].compute_log_likelihood_torch(onset_ages[i])

        # Log-acceleration random effect.
        for i in range(number_of_subjects):
            regularity += \
                self.individual_random_effects['log_acceleration'].compute_log_likelihood_torch(log_accelerations[i])

        # Noise random effect.
        for k in range(self.number_of_objects):
            regularity -= 0.5 * self.objects_noise_dimension[k] * number_of_subjects \
                          * math.log(self.fixed_effects['noise_variance'][k])

        return regularity

    def _compute_class1_priors_regularity(self):
        """
        Fully torch.
        Prior terms of the class 1 fixed effects, i.e. those for which we know a close-form update. No derivative
        wrt those fixed effects will therefore be necessary.
        """
        regularity = 0.0

        # Reference time prior.
        regularity += self.priors['reference_time'].compute_log_likelihood(self.fixed_effects['reference_time'])

        # Time-shift variance prior.
        regularity += \
            self.priors['time_shift_variance'].compute_log_likelihood(self.fixed_effects['time_shift_variance'])

        # Log-acceleration variance prior.
        regularity += self.priors['log_acceleration_variance'].compute_log_likelihood(
            self.fixed_effects['log_acceleration_variance'])

        # Noise variance prior.
        regularity += self.priors['noise_variance'].compute_log_likelihood(self.fixed_effects['noise_variance'])

        return regularity

    def _compute_class2_priors_regularity(self, template_data, control_points, momenta, modulation_matrix):
        """
        Fully torch.
        Prior terms of the class 2 fixed effects, i.e. those for which we do not know a close-form update. Derivative
        wrt those fixed effects will therefore be necessary.
        """
        regularity = 0.0

        # Prior on template_data fixed effects (if not frozen).
        if not self.freeze_template:
            regularity += self.priors['template_data'].compute_log_likelihood_torch(template_data)

        # Prior on control_points fixed effects (if not frozen).
        if not self.freeze_control_points:
            regularity += self.priors['control_points'].compute_log_likelihood_torch(control_points)

        # Prior on momenta fixed effects.
        regularity += self.priors['momenta'].compute_log_likelihood_torch(momenta)

        # Prior on modulation_matrix fixed effects.
        regularity += self.priors['modulation_matrix'].compute_log_likelihood_torch(modulation_matrix)

        return regularity

    def _compute_residuals(self, dataset, template_data, control_points, momenta, modulation_matrix,
                           sources, onset_ages, log_accelerations):
        """
        Core part of the ComputeLogLikelihood methods. Fully torch.
        """

        # Initialize: longitudinal dataset -----------------------------------------------------------------------------
        targets = dataset.deformable_objects
        absolute_times = self._compute_absolute_times(dataset.times, onset_ages.data.numpy(),
                                                      log_accelerations.data.numpy())

        # Deform -------------------------------------------------------------------------------------------------------
        residuals = []  # List of list of torch 1D tensors. Individuals, time-points, object.

        self.spatiotemporal_reference_frame.set_template_data_t0(template_data)
        self.spatiotemporal_reference_frame.set_control_points_t0(control_points)
        self.spatiotemporal_reference_frame.set_momenta_t0(momenta)
        self.spatiotemporal_reference_frame.set_modulation_matrix_t0(modulation_matrix)
        self.spatiotemporal_reference_frame.set_t0(self.get_reference_time())
        self.spatiotemporal_reference_frame.set_tmin(min([subject_times[0] for subject_times in absolute_times]))
        self.spatiotemporal_reference_frame.set_tmax(max([subject_times[-1] for subject_times in absolute_times]))
        self.spatiotemporal_reference_frame.update()

        for i in range(len(targets)):
            residuals_i = []
            for j, (time, target) in enumerate(zip(absolute_times[i], targets[i])):
                deformed_points = self.spatiotemporal_reference_frame.get_template_data(time, sources[i])
                residuals_i.append(
                    self.multi_object_attachment.compute_distances(deformed_points, self.template, target))
            residuals.append(residuals_i)

        return residuals

    def _compute_absolute_times(self, times, onset_ages, log_accelerations):
        reference_time = self.get_reference_time()
        absolute_times = []
        for i in range(len(times)):
            acceleration = math.exp(log_accelerations[i])
            absolute_times_i = []
            for j in range(len(times[i])):
                absolute_times_i.append(acceleration * (times[i][j] - onset_ages[i]) + reference_time)
            absolute_times.append(absolute_times_i)
        return absolute_times

    ####################################################################################################################
    ### Initializing methods:
    ####################################################################################################################

    def initialize_template_attributes(self, template_specifications):
        """
        Sets the Template, TemplateObjectsName, TemplateObjectsNameExtension, TemplateObjectsNorm,
        TemplateObjectsNormKernelType and TemplateObjectsNormKernelWidth attributes.
        """
        t_list, t_name, t_name_extension, t_noise_variance, t_multi_object_attachment = \
            create_template_metadata(template_specifications)

        self.template.object_list = t_list
        self.objects_name = t_name
        self.objects_name_extension = t_name_extension
        self.multi_object_attachment = t_multi_object_attachment

        self.template.update()
        self.objects_noise_dimension = compute_noise_dimension(self.template, self.multi_object_attachment)
        self.number_of_objects = len(self.template.object_list)

    def initialize_template_data_variables(self):
        """
        Terminate the initialization of the template data fixed effect, and initialize the corresponding prior.
        """
        # Propagates the initial values to the template object.
        self.set_template_data(self.template.get_points())

        # If needed (i.e. template not frozen), initialize the associated prior.
        if not self.freeze_template:
            # Set the template data prior mean as the initial template data.
            self.priors['template_data'].mean = self.get_template_data()
            # Set the template data prior standard deviation to the deformation kernel width.
            self.priors['template_data'].set_variance_sqrt(self.spatiotemporal_reference_frame.get_kernel_width())

    def initialize_control_points_variables(self):
        """
        Initialize the control points fixed effect if needed, and the associated prior.
        """
        # If needed, initialize the control points fixed effects.
        if self.fixed_effects['control_points'] is None:
            control_points = create_regular_grid_of_points(self.bounding_box, self.initial_cp_spacing)
            self.set_control_points(control_points)
            self.number_of_control_points = control_points.shape[0]
            print('>> Set of ' + str(self.number_of_control_points) + ' control points defined.')

        # If needed (i.e. control points not frozen), initialize the associated prior.
        if not self.freeze_control_points:
            # Set the control points prior mean as the initial control points.
            self.priors['control_points'].mean = self.get_control_points()
            # Set the control points prior standard deviation to the deformation kernel width.
            self.priors['control_points'].set_variance_sqrt(self.spatiotemporal_reference_frame.get_kernel_width())

    def initialize_momenta_variables(self):
        """
        Initialize the momenta fixed effect if needed, and the associated prior.
        """
        # If needed, initialize the momenta fixed effect.
        if self.fixed_effects['momenta'] is None:
            self.individual_random_effects['momenta'].mean \
                = np.zeros((self.number_of_control_points, Settings().dimension))
        # Set the momenta prior mean as the initial momenta.
        self.priors['momenta'].mean = self.get_momenta()
        # Set the momenta prior variance as the norm of the initial rkhs matrix.
        assert self.spatiotemporal_reference_frame.get_kernel_width() is not None
        dimension = Settings().dimension  # Shorthand.
        rkhs_matrix = np.zeros((self.number_of_control_points * dimension, self.number_of_control_points * dimension))
        for i in range(self.number_of_control_points):
            for j in range(self.number_of_control_points):
                cp_i = self.fixed_effects['control_points'][i, :]
                cp_j = self.fixed_effects['control_points'][j, :]
                kernel_distance = math.exp(
                    - np.sum((cp_j - cp_i) ** 2) / (self.spatiotemporal_reference_frame.get_kernel_width() ** 2))  # Gaussian kernel.
                for d in range(dimension):
                    rkhs_matrix[dimension * i + d, dimension * j + d] = kernel_distance
                    rkhs_matrix[dimension * j + d, dimension * i + d] = kernel_distance
        self.priors['momenta'].set_variance(np.linalg.norm(rkhs_matrix))  # Frobenius norm.

    def initialize_modulation_matrix_variables(self):
        # If needed, initialize the modulation matrix fixed effect.
        if self.fixed_effects['modulation_matrix'] is None:
            if self.number_of_sources is None:
                raise RuntimeError('The number of sources must be set before calling the update method '
                                   'of the LongitudinalAtlas class.')
            self.fixed_effects['modulation_matrix'] = np.zeros((self.get_control_points().size, self.number_of_sources))
        # Set the modulation_matrix prior mean as the initial modulation_matrix.
        self.priors['modulation_matrix'].mean = self.get_modulation_matrix()
        # Set the modulation_matrix prior standard deviation to the deformation kernel width.
        self.priors['modulation_matrix'].set_variance_sqrt(self.spatiotemporal_reference_frame.get_kernel_width())

    def initialize_reference_time_variables(self):
        # Check that the reference time fixed effect has been set.
        if self.fixed_effects['reference_time'] is None:
            raise RuntimeError('The reference time fixed effect of a LongitudinalAtlas model should be initialized '
                               'before calling the update method.')
        # Set the reference_time prior mean as the initial reference_time.
        self.priors['reference_time'].mean = np.zeros((1,)) + self.get_reference_time()
        # Check that the reference_time prior variance has been set.
        if self.priors['reference_time'].variance_sqrt is None:
            raise RuntimeError('The reference time prior variance of a LongitudinalAtlas model should be initialized '
                               'before calling the update method.')

    def _initialize_source_variables(self):
        # Set the sources random effect mean.
        if self.number_of_sources is None:
            raise RuntimeError('The number of sources must be set before calling the update method '
                               'of the LongitudinalAtlas class.')
        self.individual_random_effects['sources'].mean = np.zeros((self.number_of_sources,))
        # Set the sources random effect variance.
        self.individual_random_effects['sources'].set_variance(1.0)

    def _initialize_time_shift_variables(self):
        # Check that the onset age random variable mean has been set.
        if self.individual_random_effects['onset_age'].mean is None:
            raise RuntimeError('The set_reference_time method of a LongitudinalAtlas model should be called before '
                               'the update one.')
        # Check that the the onset age random variable variance has been set.
        if self.individual_random_effects['onset_age'].variance_sqrt is None:
            raise RuntimeError('The set_time_shift_variance method of a LongitudinalAtlas model should be called '
                               'before the update one.')
        # Set the time_shift_variance prior scale to the initial time_shift_variance fixed effect.
        self.priors['time_shift_variance'].scale_scalars.append(self.get_time_shift_variance())
        # Arbitrarily set the time_shift_variance prior dof to 1.
        print('>> The time shift variance prior degrees of freedom parameter is ARBITRARILY set to 1.')
        self.priors['time_shift_variance'].degrees_of_freedom.append(1.0)

    def _initialize_log_acceleration_variables(self):
        # Set the log_acceleration random variable mean.
        self.individual_random_effects['log_acceleration'].mean = np.zeros((1,))
        # Set the log_acceleration_variance fixed effect.
        print('>> The initial log-acceleration variance fixed effect is ARBITRARILY set to 0.5')
        self.set_log_acceleration_variance(0.5)
        # Set the log_acceleration_variance prior scale to the initial log_acceleration_variance fixed effect.
        self.priors['log_acceleration_variance'].scale_scalars.append(self.get_log_acceleration_variance())
        # Arbitrarily set the log_acceleration_variance prior dof to 1.
        print('>> The log-acceleration variance prior degrees of freedom parameter is ARBITRARILY set to 1.')
        self.priors['log_acceleration_variance'].degrees_of_freedom.append(1.0)

    def _initialize_noise_variables(self):
        self.set_noise_variance(np.asarray(self.priors['noise_variance'].scale_scalars))

    def _initialize_bounding_box(self):
        """
        Initialize the bounding box. which tightly encloses all template objects and the atlas control points.
        Relevant when the control points are given by the user.
        """
        self.bounding_box = self.template.bounding_box
        assert (self.number_of_control_points > 0)
        dimension = Settings().dimension
        control_points = self.get_control_points()
        for k in range(self.number_of_control_points):
            for d in range(dimension):
                if control_points[k, d] < self.bounding_box[d, 0]:
                    self.bounding_box[d, 0] = control_points[k, d]
                elif control_points[k, d] > self.bounding_box[d, 1]:
                    self.bounding_box[d, 1] = control_points[k, d]

    ####################################################################################################################
    ### Private utility methods:
    ####################################################################################################################

    def _fixed_effects_to_torch_tensors(self, with_grad):
        """
        Convert the input fixed_effects into torch tensors.
        """
        # Template data.
        template_data = self.fixed_effects['template_data']
        template_data = Variable(torch.from_numpy(template_data).type(Settings().tensor_scalar_type),
                                 requires_grad=((not self.freeze_template) and with_grad))
        # Control points.
        control_points = self.fixed_effects['control_points']
        control_points = Variable(torch.from_numpy(control_points).type(Settings().tensor_scalar_type),
                                  requires_grad=((not self.freeze_control_points) and with_grad))
        # Momenta.
        momenta = self.fixed_effects['momenta']
        momenta = Variable(torch.from_numpy(momenta).type(Settings().tensor_scalar_type), requires_grad=with_grad)
        # Modulation matrix.
        modulation_matrix = self.fixed_effects['modulation_matrix']
        modulation_matrix = Variable(torch.from_numpy(modulation_matrix).type(Settings().tensor_scalar_type),
                                     requires_grad=with_grad)

        return template_data, control_points, momenta, modulation_matrix

    def _individual_RER_to_torch_tensors(self, individual_RER, with_grad):
        """
        Convert the input individual_RER into torch tensors.
        """
        # Sources.
        sources = individual_RER['sources']
        sources = Variable(torch.from_numpy(sources).type(Settings().tensor_scalar_type), requires_grad=with_grad)
        # Onset ages.
        onset_ages = individual_RER['onset_age']
        onset_ages = Variable(torch.from_numpy(onset_ages).type(Settings().tensor_scalar_type),
                              requires_grad=with_grad)
        # Log accelerations.
        log_accelerations = individual_RER['log_acceleration']
        log_accelerations = Variable(torch.from_numpy(log_accelerations).type(Settings().tensor_scalar_type),
                                     requires_grad=with_grad)
        return sources, onset_ages, log_accelerations

    ####################################################################################################################
    ### Writing methods:
    ####################################################################################################################

    def write(self, dataset, population_RER, individual_RER):
        # We save the template, the cp, the mom and the trajectories.
        sufficient_statistics = self.compute_sufficient_statistics(dataset, population_RER, individual_RER)
        self.update_fixed_effects(dataset, sufficient_statistics)
        self._write_fixed_effects(individual_RER)
        self._write_template_to_subjects_trajectories(dataset, individual_RER)  # TODO: avoid re-deforming.

    def _write_fixed_effects(self, individual_RER):
        # Template.
        template_names = []
        for i in range(len(self.objects_name)):
            aux = self.name + "__" + self.objects_name[i] + self.objects_name_extension[i]
            template_names.append(aux)
        self.template.write(template_names)

        # Control points.
        write_2D_array(self.get_control_points(), self.name + "__control_points.txt")

        # Momenta.
        write_momenta(individual_RER['momenta'], self.name + "__momenta.txt")

        # Momenta covariance.
        write_2D_array(self.get_covariance_momenta_inverse(), self.name + "__covariance_momenta_inverse.txt")

        # Noise variance.
        write_2D_array(self.get_noise_variance(), self.name + "__noise_variance.txt")

    def _write_template_to_subjects_trajectories(self, dataset, individual_RER):
        self.diffeomorphism.set_initial_template_data_from_numpy(self.get_template_data())
        self.diffeomorphism.set_initial_control_points_from_numpy(self.get_control_points())
        for i, subject in enumerate(dataset.deformable_objects):
            names = [elt + "_to_subject_" + str(i) for elt in self.objects_name]
            self.diffeomorphism.set_initial_momenta_from_numpy(individual_RER['momenta'][i])
            self.diffeomorphism.update()
            self.diffeomorphism.write_flow(names, self.objects_name_extension, self.template)
