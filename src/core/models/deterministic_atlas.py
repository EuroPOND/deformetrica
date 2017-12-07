import os.path
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + os.path.sep + '../../')

from os.path import splitext
import numpy as np
import math

import torch
from torch.autograd import Variable

from pydeformetrica.src.core.models.abstract_statistical_model import AbstractStatisticalModel
from pydeformetrica.src.in_out.deformable_object_reader import DeformableObjectReader
from pydeformetrica.src.core.model_tools.deformations.diffeomorphism import Diffeomorphism
from pydeformetrica.src.core.observations.deformable_objects.deformable_multi_object import DeformableMultiObject
from pydeformetrica.src.support.utilities.general_settings import GeneralSettings
from pydeformetrica.src.support.utilities.torch_kernel import TorchKernel
from pydeformetrica.src.in_out.utils import *
from pydeformetrica.src.core.model_tools.attachments.multi_object_attachment import ComputeMultiObjectDistance

class DeterministicAtlas(AbstractStatisticalModel):

    """
    Deterministic atlas object class.

    """

    ################################################################################
    ### Constructor:
    ################################################################################

    def __init__(self):
        self.Template = DeformableMultiObject()
        self.ObjectsName = []
        self.ObjectsNameExtension = []
        self.ObjectsNoiseVariance = []
        self.ObjectsNorm = []
        self.ObjectsNormKernelType = []
        self.ObjectsNormKernelWidth = []

        self.Diffeomorphism = Diffeomorphism()

        self.SmoothingKernelWidth = None
        self.InitialCpSpacing = None
        self.NumberOfSubjects = None
        self.NumberOfObjects = None
        self.NumberOfControlPoints = None
        self.BoundingBox = None

        # Numpy arrays.
        self.FixedEffects = {}
        self.FixedEffects['TemplateData'] = None
        self.FixedEffects['ControlPoints'] = None
        self.FixedEffects['Momenta'] = None


    ################################################################################
    ### Encapsulation methods:
    ################################################################################

    # Those methods do the numpy/torch conversion.
    def GetTemplateData(self):
        return Variable(torch.from_numpy(self.FixedEffects['TemplateData']), requires_grad = True)
    def SetTemplateData(self, td):
        self.FixedEffects['TemplateData'] = td.data.numpy()
        self.Template.SetPoints(self.FixedEffects['TemplateData'])

    def GetControlPoints(self):
        return Variable(torch.from_numpy(self.FixedEffects['ControlPoints']), requires_grad = True)
    def SetControlPoints(self, cp):
        self.FixedEffects['ControlPoints'] = cp.data.numpy()

    def GetMomenta(self):
        return Variable(torch.from_numpy(self.FixedEffects['Momenta']), requires_grad = True)
    def SetMomenta(self, mom):
        self.FixedEffects['Momenta'] = mom.data.numpy()

    # From vectorized torch tensor.
    def SetFixedEffects(self, fixedEffects):
        td, cp, mom = self.UnvectorizeFixedEffects(fixedEffects)
        self.SetTemplateData(td)
        self.SetControlPoints(cp)
        self.SetMomenta(mom)


    ################################################################################
    ### Public methods:
    ################################################################################

    # Final initialization steps.
    def Update(self):

        self.Template.Update()

        self.NumberOfObjects = len(self.Template.ObjectList)
        self.BoundingBox = self.Template.BoundingBox

        self.SetTemplateData(self.Template.GetData().Concatenate())
        if self.FixedEffects['ControlPoints'] is None: self.InitializeControlPoints()
        else: self.InitializeBoundingBox()
        if self.FixedEffects['Momenta'] is None: self.InitializeMomenta()

    # Compute the functional. Fully torch function.
    def ComputeLogLikelihood(self, dataset, fixedEffects, popRER, indRER):

        # Initialize ---------------------------------------------------------------
        templateData, controlPoints, momenta = self.UnvectorizeFixedEffects(fixedEffects)
        targets = dataset.DeformableObjects
        targets = [target[0] for target in targets] # Cross-sectional data.
        targetsData = [target.GetData().Concatenate() for target in targets]

        # Deform -------------------------------------------------------------------
        regularity = 0.
        attachment = 0.

        self.Diffeomorphism.SetLandmarkPoints(templateData)
        for i, targetData in enumerate(targetsData):
            self.Diffeomorphism.SetStartPositions(controlPoints)
            self.Diffeomorphism.SetStartMomenta(momenta[i])
            self.Diffeomorphism.Shoot()
            self.Diffeomorphism.Flow()
            deformedPoints = self.Diffeomorphism.GetLandmarkPoints()
            regularity += self.Diffeomorphism.GetNorm()
            attachment += ComputeMultiObjectDistance(
                deformedPoints, targetData, self.Template, targets[i], kernel_width=self.ObjectsNormKernelWidth)
        return regularity + self.ObjectsNoiseVariance[0] * attachment

    # Numpy input, torch output.
    def GetVectorizedFixedEffects(self):
        # Torch tensors.
        templateData = self.GetTemplateData()
        controlPoints = self.GetControlPoints()
        momenta = self.GetMomenta()

        # The order decided here must be consistent with the unvectorize method.
        return np.concatenate((templateData.flatten(), controlPoints.flatten(), momenta.flatten()))

    # Fully torch method.
    def UnvectorizeFixedEffects(self, fixedEffects):
        (a_td, b_td) = self.FixedEffects['TemplateData'].shape
        templateData = fixedEffects[:a_td*b_td].view(a_td, b_td)

        (a_cp, b_cp) = self.FixedEffects['ControlPoints'].shape
        controlPoints = fixedEffects[a_td*b_td:a_td*b_td + a_cp*b_cp].view(a_cp, b_cp)

        (a_mom, b_mom, c_mom) = self.FixedEffects['Momenta'].shape
        momenta = fixedEffects[a_td*b_td + a_cp*b_cp:].view(a_mom, b_mom, c_mom)

        return templateData, controlPoints, momenta


    ################################################################################
    ### Private methods:
    ################################################################################

    # Sets the Template, TemplateObjectsName, TemplateObjectsNameExtension, TemplateObjectsNorm,
    # TemplateObjectsNormKernelType and TemplateObjectsNormKernelWidth attributes.
    def InitializeTemplateAttributes(self, templateSpecifications):

        for object_id, object in templateSpecifications.iteritems():
            filename = object['Filename']
            objectType = object['DeformableObjectType'].lower()

            root, extension = splitext(filename)
            reader = DeformableObjectReader()

            self.Template.ObjectList.append(reader.CreateObject(filename, objectType))
            self.ObjectsName.append(object_id)
            self.ObjectsNameExtension.append(extension)
            self.ObjectsNoiseVariance.append(object['NoiseStd']**2)

            if objectType == 'OrientedSurfaceMesh'.lower():
                self.ObjectsNorm.append('Current')
                self.ObjectsNormKernelType.append(object['KernelType'])
                self.ObjectsNormKernelWidth.append(float(object['KernelWidth']))
            elif objectType == 'NonOrientedSurfaceMesh'.lower():
                self.ObjectsNorm.append('Varifold')
                self.ObjectsNormKernelType.append(object['KernelType'])
                self.ObjectsNormKernelWidth.append(float(object['KernelWidth']))
            else:
                raise RuntimeError('In DeterminiticAtlas.InitializeTemplateAttributes: '
                                   'unknown object type: ' + objectType)

        self.SetTemplateData(self.Template.GetData().Concatenate())


    # Initialize the control points fixed effect.
    def InitializeControlPoints(self):
        dimension = GeneralSettings.Instance().Dimension

        axis = []
        for d in range(dimension):
            min = self.BoundingBox[d, 0]
            max = self.BoundingBox[d, 1]
            length = max - min
            assert(length > 0)

            offset = 0.5 * (length - self.InitialCpSpacing * math.floor(length)) / self.InitialCpSpacing
            axis.append(np.arange(min + offset, max, self.InitialCpSpacing))

        if dimension == 2:
            xAxis, yAxis = np.meshgrid(axis[0], axis[1])

            assert (xAxis.shape == yAxis.shape)
            self.NumberOfControlPoints = xAxis.flatten().shape[0]
            controlPoints = np.zeros((self.NumberOfControlPoints, dimension))

            controlPoints[:, 0] = xAxis.flatten()
            controlPoints[:, 1] = yAxis.flatten()

        elif dimension == 3:
            xAxis, yAxis, zAxis = np.meshgrid(axis[0], axis[1], axis[2])

            assert (xAxis.shape == yAxis.shape)
            assert (xAxis.shape == zAxis.shape)
            self.NumberOfControlPoints = xAxis.flatten().shape[0]
            controlPoints = np.zeros((self.NumberOfControlPoints, dimension))

            controlPoints[:, 0] = xAxis.flatten()
            controlPoints[:, 1] = yAxis.flatten()
            controlPoints[:, 2] = zAxis.flatten()

        else:
            raise RuntimeError('In DeterministicAtlas.InitializeControlPoints: invalid ambient space dimension.')

        self.SetControlPoints(controlPoints)
        print('>> Set of ' + str(self.NumberOfControlPoints) + ' control points defined.')

    # Initialize the momenta fixed effect.
    def InitializeMomenta(self):
        assert(self.NumberOfSubjects > 0)
        momenta = np.zeros((self.NumberOfSubjects, self.NumberOfControlPoints, GeneralSettings.Instance().Dimension))
        self.SetMomenta(momenta)
        print('>> Deterministic atlas momenta initialized to zero, for ' + str(self.NumberOfSubjects) + ' subjects.')

    # Initialize the bounding box. which tightly encloses all template objects and the atlas control points.
    # Relevant when the control points are given by the user.
    def InitializeBoundingBox(self):
        assert(self.NumberOfControlPoints > 0)

        dimension = GeneralSettings.Instance().Dimension
        controlPoints = self.GetControlPoints()

        for k in range(self.NumberOfControlPoints):
            for d in range(dimension):
                if controlPoints[k, d] < self.BoundingBox[d, 0]: self.BoundingBox[d, 0] = controlPoints[k, d]
                elif controlPoints[k, d] > self.BoundingBox[d, 1]: self.BoundingBox[d, 1] = controlPoints[k, d]

    def WriteTemplate(self):
        templateNames = []
        for i in range(len(self.ObjectsName)):
            aux = "Atlas_" + self.ObjectsName[i] + self.ObjectsNameExtension[i]
            aux = os.path.join(GeneralSettings.Instance().OutputDir, aux)
            templateNames.append(aux)
        self.Template.Write(templateNames)

    def WriteControlPoints(self):
        saveArray(self.GetControlPoints(), "Atlas_ControlPoints.txt")

    def WriteMomenta(self):
        saveMomenta(self.GetMomenta(), "Atlas_Momenta.txt")

    def WriteTemplateToSubjectsTrajectories(self):
        pass

    def Write(self):
        #We save the template, the cp, the mom and the trajectories
        self.WriteTemplate()
        self.WriteControlPoints()
        self.WriteMomenta()
        self.WriteTemplateToSubjectsTrajectories()
