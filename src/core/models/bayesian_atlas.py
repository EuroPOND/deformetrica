import math

import torch
from torch.autograd import Variable

from core.model_tools.deformations.exponential import Exponential
from core.models.abstract_statistical_model import AbstractStatisticalModel
from core.models.model_functions import create_regular_grid_of_points, compute_sobolev_gradient
from core.observations.deformable_objects.deformable_multi_object import DeformableMultiObject
from in_out.array_readers_and_writers import *
from in_out.dataset_functions import create_template_metadata, compute_noise_dimension
from support.probability_distributions.inverse_wishart_distribution import InverseWishartDistribution
from support.probability_distributions.multi_scalar_inverse_wishart_distribution import \
    MultiScalarInverseWishartDistribution
from support.probability_distributions.normal_distribution import NormalDistribution

import logging
logger = logging.getLogger(__name__)


class BayesianAtlas(AbstractStatisticalModel):
    """
    Bayesian atlas object class.
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
        self.exponential = Exponential()

        self.use_sobolev_gradient = True
        self.smoothing_kernel_width = None

        self.initial_cp_spacing = None
        self.number_of_objects = None
        self.number_of_control_points = None
        self.bounding_box = None

        # Dictionary of numpy arrays.
        self.fixed_effects['template_data'] = None
        self.fixed_effects['control_points'] = None
        self.fixed_effects['covariance_momenta_inverse'] = None
        self.fixed_effects['noise_variance'] = None

        # Dictionary of probability distributions.
        self.priors['covariance_momenta'] = InverseWishartDistribution()
        self.priors['noise_variance'] = MultiScalarInverseWishartDistribution()

        # Dictionary of probability distributions.
        self.individual_random_effects['momenta'] = NormalDistribution()

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

    # Covariance momenta inverse ---------------------------------------------------------------------------------------
    def get_covariance_momenta_inverse(self):
        return self.fixed_effects['covariance_momenta_inverse']

    def set_covariance_momenta_inverse(self, cmi):
        self.fixed_effects['covariance_momenta_inverse'] = cmi
        self.individual_random_effects['momenta'].set_covariance_inverse(cmi)

    def set_covariance_momenta(self, cm):
        self.set_covariance_momenta_inverse(np.linalg.inv(cm))

    # Noise variance ---------------------------------------------------------------------------------------------------
    def get_noise_variance(self):
        return self.fixed_effects['noise_variance']

    def set_noise_variance(self, nv):
        self.fixed_effects['noise_variance'] = nv

    # Full fixed effects -----------------------------------------------------------------------------------------------
    def get_fixed_effects(self):
        out = {}
        if not self.freeze_template:
            for key, value in self.fixed_effects['template_data'].items():
                out[key] = value
        if not self.freeze_control_points:
            out['control_points'] = self.fixed_effects['control_points']
        return out

    def set_fixed_effects(self, fixed_effects):
        if not self.freeze_template:
            template_data = {key: fixed_effects[key] for key in self.fixed_effects['template_data'].keys()}
            self.set_template_data(template_data)
        if not self.freeze_control_points:
            self.set_control_points(fixed_effects['control_points'])

    ####################################################################################################################
    ### Public methods:
    ####################################################################################################################

    def update(self):
        """
        Final initialization steps.
        """
        self.number_of_objects = len(self.template.object_list)
        self.bounding_box = self.template.bounding_box

        self.set_template_data(self.template.get_data())

        if self.fixed_effects['control_points'] is None:
            self._initialize_control_points()
        else:
            self._initialize_bounding_box()

        self._initialize_momenta()
        self._initialize_noise_variance()

    def compute_log_likelihood(self, dataset, population_RER, individual_RER, mode='complete', with_grad=False):
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
        template_data, template_points, control_points = self._fixed_effects_to_torch_tensors(with_grad)
        momenta = self._individual_RER_to_torch_tensors(individual_RER, with_grad and mode == 'complete')

        # Deform, update, compute metrics ------------------------------------------------------------------------------
        residuals = self._compute_residuals(dataset, template_data, template_points, control_points, momenta)

        # Update the fixed effects only if the user asked for the complete log likelihood.
        if mode == 'complete':
            sufficient_statistics = self.compute_sufficient_statistics(dataset, population_RER, individual_RER,
                                                                       residuals=residuals)
            self.update_fixed_effects(dataset, sufficient_statistics)

        # Compute the attachment, with the updated noise variance parameter in the 'complete' mode.
        attachments = self._compute_individual_attachments(residuals)
        attachment = torch.sum(attachments)

        # Compute the regularity terms according to the mode.
        regularity = 0.0
        if mode == 'complete':
            regularity = self._compute_random_effects_regularity(momenta)
            regularity += self._compute_class1_priors_regularity()
        if mode in ['complete', 'class2']:
            regularity += self._compute_class2_priors_regularity(template_data, control_points)

        # Compute gradient if needed -----------------------------------------------------------------------------------
        if with_grad:
            total = regularity + attachment
            total.backward()

            gradient = {}
            gradient_numpy = {}

            # Template data.
            if not self.freeze_template:
                if 'landmark_points' in template_data.keys():
                    gradient['landmark_points'] = template_points['landmark_points'].grad
                if 'image_intensities' in template_data.keys():
                    gradient['image_intensities'] = template_data['image_intensities'].grad
                # for key, value in template_data.items():
                #     if value.grad is not None:
                #         gradient[key] = value.grad

                if self.use_sobolev_gradient and 'landmark_points' in gradient.keys():
                    gradient['landmark_points'] = compute_sobolev_gradient(
                        gradient['landmark_points'], self.smoothing_kernel_width, self.template)

            # Control points.
            if not self.freeze_control_points: gradient['control_points'] = control_points.grad

            # Individual effects.
            if mode == 'complete': gradient['momenta'] = momenta.grad

            # Convert to numpy.
            for (key, value) in gradient.items(): gradient_numpy[key] = value.data.cpu().numpy()

            # Return as appropriate.
            if mode in ['complete', 'class2']:
                return attachment.detach().cpu().numpy(), regularity.detach().cpu().numpy(), gradient_numpy
            elif mode == 'model':
                return attachments.detach().cpu().numpy(), gradient_numpy

        else:
            if mode in ['complete', 'class2']:
                return attachment.detach().cpu().numpy(), regularity.detach().cpu().numpy()
            elif mode == 'model':
                return attachments.detach().cpu().numpy()

    def compute_sufficient_statistics(self, dataset, population_RER, individual_RER, residuals=None):
        """
        Compute the model sufficient statistics.
        """
        if residuals is None:
            # Initialize: conversion from numpy to torch ---------------------------------------------------------------
            # Template data.
            template_data = self.fixed_effects['template_data']
            template_data = Variable(torch.from_numpy(template_data).type(Settings().tensor_scalar_type),
                                     requires_grad=False)
            # Control points.
            control_points = self.fixed_effects['control_points']
            control_points = Variable(torch.from_numpy(control_points).type(Settings().tensor_scalar_type),
                                      requires_grad=False)
            # Momenta.
            momenta = individual_RER['momenta']
            momenta = Variable(torch.from_numpy(momenta).type(Settings().tensor_scalar_type), requires_grad=False)

            # Compute residuals ----------------------------------------------------------------------------------------
            residuals = [torch.sum(residuals_i)
                         for residuals_i in self._compute_residuals(dataset, template_data, control_points, momenta)]

        # Compute sufficient statistics --------------------------------------------------------------------------------
        sufficient_statistics = {}

        # Empirical momenta covariance.
        momenta = individual_RER['momenta']
        sufficient_statistics['S1'] = np.zeros((momenta[0].size, momenta[0].size))
        for i in range(dataset.number_of_subjects):
            sufficient_statistics['S1'] += np.dot(momenta[i].reshape(-1, 1), momenta[i].reshape(-1, 1).transpose())

        # Empirical residuals variances, for each object.
        sufficient_statistics['S2'] = np.zeros((self.number_of_objects,))
        for k in range(self.number_of_objects):
            sufficient_statistics['S2'][k] = residuals[k].detach().cpu().numpy()

        # Finalization -------------------------------------------------------------------------------------------------
        return sufficient_statistics

    def update_fixed_effects(self, dataset, sufficient_statistics):
        """
        Updates the fixed effects based on the sufficient statistics, maximizing the likelihood.
        """
        # Covariance of the momenta update.
        prior_scale_matrix = self.priors['covariance_momenta'].scale_matrix
        prior_dof = self.priors['covariance_momenta'].degrees_of_freedom
        covariance_momenta = sufficient_statistics['S1'] + prior_dof * np.transpose(prior_scale_matrix) \
                                                           / (dataset.number_of_subjects + prior_dof)
        self.set_covariance_momenta(covariance_momenta)

        # Variance of the residual noise update.
        noise_variance = np.zeros((self.number_of_objects,))
        prior_scale_scalars = self.priors['noise_variance'].scale_scalars
        prior_dofs = self.priors['noise_variance'].degrees_of_freedom
        for k in range(self.number_of_objects):
            noise_variance[k] = (sufficient_statistics['S2'] + prior_scale_scalars[k] * prior_dofs[k]) \
                                / float(dataset.number_of_subjects * self.objects_noise_dimension[k] + prior_dofs[k])
        self.set_noise_variance(noise_variance)

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

    ####################################################################################################################
    ### Private methods:
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
            attachments[i] = - 0.5 * torch.sum(residuals[i] / Variable(
                torch.from_numpy(self.fixed_effects['noise_variance']).type(Settings().tensor_scalar_type),
                requires_grad=False))
        return attachments

    def _compute_random_effects_regularity(self, momenta):
        """
        Fully torch.
        """
        number_of_subjects = momenta.shape[0]
        regularity = 0.0

        # Momenta random effect.
        for i in range(number_of_subjects):
            regularity += self.individual_random_effects['momenta'].compute_log_likelihood_torch(momenta[i])

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

        # Covariance momenta prior.
        regularity += self.priors['covariance_momenta'].compute_log_likelihood(
            self.fixed_effects['covariance_momenta_inverse'])

        # Noise variance prior.
        regularity += self.priors['noise_variance'].compute_log_likelihood(self.fixed_effects['noise_variance'])

        return regularity

    def _compute_class2_priors_regularity(self, template_data, control_points):
        """
        Fully torch.
        Prior terms of the class 2 fixed effects, i.e. those for which we do not know a close-form update. Derivative
        wrt those fixed effects will therefore be necessary.
        """
        regularity = 0.0

        # Prior on template_data fixed effects (if not frozen). None implemented yet TODO.
        if not self.freeze_template:
            regularity += 0.0

        # Prior on control_points fixed effects (if not frozen). None implemented yet TODO.
        if not self.freeze_control_points:
            regularity += 0.0

        return regularity

    def _compute_residuals(self, dataset, template_data, template_points, control_points, momenta):
        """
        Core part of the ComputeLogLikelihood methods. Fully torch.
        """

        # Initialize: cross-sectional dataset --------------------------------------------------------------------------
        targets = dataset.deformable_objects
        targets = [target[0] for target in targets]

        # Deform -------------------------------------------------------------------------------------------------------
        residuals = []

        self.exponential.set_initial_template_points(template_points)
        self.exponential.set_initial_control_points(control_points)

        for i, target in enumerate(targets):
            self.exponential.set_initial_momenta(momenta[i])
            self.exponential.update()
            deformed_points = self.exponential.get_template_points()
            deformed_data = self.template.get_deformed_data(deformed_points, template_data)
            residuals.append(self.multi_object_attachment.compute_distances(deformed_data, self.template, target))

        return residuals

    def _initialize_control_points(self):
        """
        Initialize the control points fixed effect.
        """
        if not Settings().dense_mode:
            control_points = create_regular_grid_of_points(self.bounding_box, self.initial_cp_spacing)
        else:
            control_points = self.template.get_points()

        self.set_control_points(control_points)
        self.number_of_control_points = control_points.shape[0]
        logger.info('Set of ' + str(self.number_of_control_points) + ' control points defined.')

    def _initialize_momenta(self):
        """
        Initialize the momenta fixed effect.
        """
        self.individual_random_effects['momenta'].mean = \
            np.zeros((self.number_of_control_points * Settings().dimension,))
        self._initialize_covariance()  # Initialize the prior and the momenta random effect.

    def _initialize_covariance(self):
        """
        Initialize the scale matrix of the inverse wishart prior, as well as the covariance matrix of the normal
        random effect.
        """
        assert self.exponential.kernel.kernel_width is not None
        dimension = Settings().dimension  # Shorthand.
        rkhs_matrix = np.zeros((self.number_of_control_points * dimension, self.number_of_control_points * dimension))
        for i in range(self.number_of_control_points):
            for j in range(self.number_of_control_points):
                cp_i = self.fixed_effects['control_points'][i, :]
                cp_j = self.fixed_effects['control_points'][j, :]
                kernel_distance = math.exp(
                    - np.sum((cp_j - cp_i) ** 2) / (self.exponential.kernel.kernel_width ** 2))  # Gaussian kernel.
                for d in range(dimension):
                    rkhs_matrix[dimension * i + d, dimension * j + d] = kernel_distance
                    rkhs_matrix[dimension * j + d, dimension * i + d] = kernel_distance
        self.priors['covariance_momenta'].scale_matrix = np.linalg.inv(rkhs_matrix)
        self.set_covariance_momenta_inverse(rkhs_matrix)

    def _initialize_noise_variance(self):
        self.set_noise_variance(np.asarray(self.priors['noise_variance'].scale_scalars))

    def _initialize_bounding_box(self):
        """
        Initialize the bounding box. which tightly encloses all template objects and the atlas control points.
        Relevant when the control points are given by the user.
        """

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
        template_data = {key: Variable(torch.from_numpy(value).type(Settings().tensor_scalar_type),
                                       requires_grad=(not self.freeze_template and with_grad))
                         for key, value in template_data.items()}

        # Template points.
        template_points = self.template.get_points()
        template_points = {key: Variable(torch.from_numpy(value).type(Settings().tensor_scalar_type),
                                         requires_grad=(not self.freeze_template and with_grad))
                           for key, value in template_points.items()}
        # Control points.
        if Settings().dense_mode:
            control_points = template_data
        else:
            control_points = self.fixed_effects['control_points']
            control_points = Variable(torch.from_numpy(control_points).type(Settings().tensor_scalar_type),
                                      requires_grad=((not self.freeze_control_points) and with_grad))

        return template_data, template_points, control_points

    def _individual_RER_to_torch_tensors(self, individual_RER, with_grad):
        """
        Convert the input individual_RER into torch tensors.
        """
        # Momenta.
        momenta = individual_RER['momenta']
        momenta = torch.from_numpy(momenta).requires_grad_(with_grad).type(Settings().tensor_scalar_type)
        return momenta

    ####################################################################################################################
    ### Printing and writing methods:
    ####################################################################################################################

    def print(self, individual_RER):
        pass

    def write(self, dataset, population_RER, individual_RER, update_fixed_effects=True, write_residuals=True):

        # Write the model predictions, and compute the residuals at the same time.
        residuals = self._write_model_predictions(dataset, individual_RER,
                                                  compute_residuals=(update_fixed_effects or write_residuals))

        # Optionally update the fixed effects.
        if update_fixed_effects:
            sufficient_statistics = self.compute_sufficient_statistics(dataset, population_RER, individual_RER,
                                                                       residuals=residuals)
            self.update_fixed_effects(dataset, sufficient_statistics)

        # Write residuals.
        if write_residuals:
            residuals_list = [[residuals_i_k.detach().cpu().numpy() for residuals_i_k in residuals_i]
                              for residuals_i in residuals]
            write_2D_list(residuals_list, self.name + "__EstimatedParameters__Residuals.txt")

        # Write the model parameters.
        self._write_model_parameters(individual_RER)

    def _write_model_predictions(self, dataset, individual_RER, compute_residuals=True):

        # Initialize.
        template_data, template_points, control_points = self._fixed_effects_to_torch_tensors(False)
        momenta = self._individual_RER_to_torch_tensors(individual_RER, False)

        # Deform, write reconstructions and compute residuals.
        self.exponential.set_initial_template_points(template_points)
        self.exponential.set_initial_control_points(control_points)

        residuals = []  # List of torch 1D tensors. Individuals, objects.
        for i, subject_id in enumerate(dataset.subject_ids):
            self.exponential.set_initial_momenta(momenta[i])
            self.exponential.update()

            deformed_points = self.exponential.get_template_points()
            deformed_data = self.template.get_deformed_data(deformed_points, template_data)

            if compute_residuals:
                residuals.append(self.multi_object_attachment.compute_distances(
                    deformed_data, self.template, dataset.deformable_objects[i][0]))

            names = []
            for k, (object_name, object_extension) \
                    in enumerate(zip(self.objects_name, self.objects_name_extension)):
                name = self.name + '__Reconstruction__' + object_name + '__subject_' + subject_id + object_extension
                names.append(name)
            self.template.write(names, {key: value.data.cpu().numpy() for key, value in deformed_data.items()})

        return residuals

    def _write_model_parameters(self, individual_RER):
        # Template.
        template_names = []
        for i in range(len(self.objects_name)):
            aux = self.name + "__EstimatedParameters__Template_" + self.objects_name[i] + self.objects_name_extension[i]
            template_names.append(aux)
        self.template.write(template_names)

        # Control points.
        write_2D_array(self.get_control_points(), self.name + "__EstimatedParameters__ControlPoints.txt")

        # Momenta.
        write_3D_array(individual_RER['momenta'], self.name + "__EstimatedParameters__Momenta.txt")

        # Momenta covariance.
        write_2D_array(self.get_covariance_momenta_inverse(),
                       self.name + "__EstimatedParameters__CovarianceMomentaInverse.txt")

        # Noise variance.
        write_2D_array(np.sqrt(self.get_noise_variance()), self.name + "__EstimatedParameters__NoiseStd.txt")
