import logging
import unittest

import torch

import support.kernels as kernel_factory
from support.utilities.general_settings import Settings


class KernelFactory(unittest.TestCase):

    def test_instantiate_abstract_class(self):
        with self.assertRaises(TypeError):
            kernel_factory.AbstractKernel()

    def test_unknown_kernel_string(self):
        with self.assertRaises(TypeError):
            kernel_factory.factory('unknown_type')

    def test_non_cuda_kernel_factory(self):
        for k in [kernel_factory.Type.NO_KERNEL, kernel_factory.Type.TORCH]:
            logging.debug("testing kernel=", k)
            instance = kernel_factory.factory(k, kernel_width=1.)
            self.__isKernelValid(instance)

    @unittest.skipIf(not torch.cuda.is_available(), 'cuda is not available')
    def test_cuda_kernel_factory(self):
        for k in [kernel_factory.Type.KEOPS, kernel_factory.Type.TORCH_CUDA]:
            logging.debug("testing kernel=", k)
            instance = kernel_factory.factory(k, kernel_width=1.)
            self.__isKernelValid(instance)

    def test_non_cuda_kernel_factory_from_string(self):
        for k in ['no_kernel', 'no-kernel', 'torch']:
            logging.debug("testing kernel=", k)
            instance = kernel_factory.factory(k, kernel_width=1.)
            self.__isKernelValid(instance)

    @unittest.skipIf(not torch.cuda.is_available(), 'cuda is not available')
    def test_cuda_kernel_factory_from_string(self):
        for k in ['keops']:
            logging.debug("testing kernel=", k)
            instance = kernel_factory.factory(k, kernel_width=1.)
            self.__isKernelValid(instance)

    def __isKernelValid(self, instance):
        if instance is not None:
            self.assertIsInstance(instance, kernel_factory.AbstractKernel)
            self.assertEqual(instance.kernel_width, 1.)


class KernelTestBase(unittest.TestCase):
    def setUp(self):
        Settings().dimension = 3

        torch.manual_seed(42)  # for reproducibility
        torch.set_printoptions(precision=30)    # for more precision when printing tensor

        self.x = torch.rand([4, 3])
        self.y = torch.rand([4, 3])
        self.p = torch.rand([4, 3])
        self.expected_convolve_res = torch.tensor([
            [1.098455786705017089843750000000, 0.841387629508972167968750000000, 1.207388281822204589843750000000],
            [1.135044455528259277343750000000, 0.859343230724334716796875000000, 1.387768864631652832031250000000],
            [1.258846044540405273437500000000, 0.927951514720916748046875000000, 1.383145928382873535156250000000],
            [1.334064722061157226562500000000, 0.887639760971069335937500000000, 1.360100984573364257812500000000]])

        self.expected_convolve_gradient_res = torch.tensor([
            [-1.623382568359375000000000000000, -1.212645769119262695312500000000, 1.440739274024963378906250000000],
            [-1.414733767509460449218750000000, 1.848072409629821777343750000000, -0.102501690387725830078125000000],
            [1.248104929924011230468750000000, 0.059575259685516357421875000000, -1.860013246536254882812500000000],
            [1.790011405944824218750000000000, -0.695001959800720214843750000000, 0.521775603294372558593750000000]])

        super().setUp()


@unittest.skipIf(not torch.cuda.is_available(), 'cuda is not available')
class Kernel(KernelTestBase):
    def setUp(self):
        self.test_on_device = 'cuda:0'
        self.kernel_instance = kernel_factory.factory(kernel_factory.Type.TorchCudaKernel,
                                                      kernel_width=1., device=self.test_on_device)
        super().setUp()

    def test_torch_cuda_with_move_to_device(self):
        res = self.kernel_instance.convolve(self.x, self.y, self.p)
        self.assertEqual(res.device, torch.device(self.test_on_device))
        # move to CPU
        res = res.to(torch.device('cpu'))
        # torch.set_printoptions(precision=25)
        # print(res)
        # print(self.expected_convolve_res)
        # print(res - self.expected_convolve_res)
        self.assertTrue(torch.equal(self.expected_convolve_res, res), 'convolve did not produce expected result')

        # test convolve gradient method
        res = self.kernel_instance.convolve_gradient(self.x, self.x)
        self.assertEqual(res.device, torch.device(self.test_on_device))
        # move to CPU
        res = res.to(torch.device('cpu'))
        # print(res)
        self.assertTrue(torch.equal(self.expected_convolve_gradient_res, res), 'convolve_gradient did not produce expected result')

    def test_torch_cuda_without_move_to_device(self):
        res = self.kernel_instance.convolve(self.x, self.y, self.p)
        self.assertEqual(res.device, torch.device(self.test_on_device))
        # move to CPU
        res = res.to(torch.device('cpu'))
        # torch.set_printoptions(precision=25)
        # print(res)
        # print(expected_convolve_res)
        # print(res - expected_convolve_res)
        self.assertTrue(torch.equal(self.expected_convolve_res, res), 'convolve did not produce expected result')

        # test convolve gradient method
        res = self.kernel_instance.convolve_gradient(self.x, self.x)
        self.assertEqual(res.device, torch.device(self.test_on_device))
        # move to CPU
        res = res.to(torch.device('cpu'))
        # print(res)
        self.assertTrue(torch.equal(self.expected_convolve_gradient_res, res), 'convolve_gradient did not produce expected result')


class KeopsKernel(KernelTestBase):
    def setUp(self):
        self.kernel_instance = kernel_factory.factory(kernel_factory.Type.KEOPS, kernel_width=1.)
        super().setUp()

    def test_convolve(self):
        res = self.kernel_instance.convolve(self.x, self.y, self.p)
        self.assertTrue(torch.equal(self.expected_convolve_res, res), 'convolve did not produce expected result')

    def test_convolve_gradient(self):
        res = self.kernel_instance.convolve_gradient(self.x, self.x)
        print(res)
        print(self.expected_convolve_gradient_res)
        self.assertTrue(torch.equal(self.expected_convolve_gradient_res, res), 'convolve_gradient did not produce expected result')

