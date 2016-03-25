import gzip
import logging
import os
import unittest
import uuid

import mock

from six.moves import builtins

import storage_utils
from storage_utils import NamedTemporaryDirectory
from storage_utils import Path
from storage_utils import swift
from storage_utils.tests.shared import assert_same_data


class BaseIntegrationTest(unittest.TestCase):
    def setUp(self):
        super(BaseIntegrationTest, self).setUp()

        if not os.environ.get('SWIFT_TEST_USERNAME'):
            raise unittest.SkipTest(
                'SWIFT_TEST_USERNAME env var not set. Skipping integration test')

        # Disable loggers so nose output wont be trashed
        logging.getLogger('requests').setLevel(logging.CRITICAL)
        logging.getLogger('swiftclient').setLevel(logging.CRITICAL)
        logging.getLogger('keystoneclient').setLevel(logging.CRITICAL)

        swift.update_settings(username=os.environ.get('SWIFT_TEST_USERNAME'),
                              password=os.environ.get('SWIFT_TEST_PASSWORD'),
                              num_retries=5)

        self.test_container = Path('swift://%s/%s' % ('AUTH_swft_test', uuid.uuid4()))
        if self.test_container.exists():
            raise ValueError('test container %s already exists.' % self.test_container)

        try:
            self.test_container.post()
        except:
            self.test_container.rmtree()
            raise

    def tearDown(self):
        super(BaseIntegrationTest, self).tearDown()
        self.test_container.rmtree()

    def get_dataset_obj_names(self, num_test_files):
        """Returns the name of objects in a test dataset generated with create_dataset"""
        return ['%s' % name for name in range(num_test_files)]

    def get_dataset_obj_contents(self, which_test_file, min_object_size):
        """Returns the object contents from a test file generated with create_dataset"""
        return '%s' % str(which_test_file) * min_object_size

    def create_dataset(self, directory, num_objects, min_object_size):
        """Creates a test dataset with predicatable names and contents

        Files are named from 0 to num_objects (exclusive), and their contents
        is file_name * min_object_size. Note that the actual object size is
        dependent on the object name and should be taken into consideration
        when testing.
        """
        with Path(directory):
            for name in self.get_dataset_obj_names(num_objects):
                with builtins.open(name, 'w') as f:
                    f.write(self.get_dataset_obj_contents(name, min_object_size))

    def assertCorrectObjectContents(self, test_obj_path, which_test_obj, min_obj_size):
        """
        Given a test object and the minimum object size used with create_dataset, assert
        that a file exists with the correct contents
        """
        with builtins.open(test_obj_path, 'r') as test_obj:
            contents = test_obj.read()
            expected = self.get_dataset_obj_contents(which_test_obj, min_obj_size)
            self.assertEquals(contents, expected)


class SwiftIntegrationTest(BaseIntegrationTest):
    def test_cached_auth_and_auth_invalidation(self):
        from swiftclient.client import get_auth_keystone as real_get_keystone
        swift._clear_cached_auth_credentials()
        with mock.patch('swiftclient.client.get_auth_keystone', autospec=True) as mock_get_ks:
            mock_get_ks.side_effect = real_get_keystone
            s = Path(self.test_container).stat()
            self.assertEquals(s['Account'], 'AUTH_swft_test')
            self.assertEquals(len(mock_get_ks.call_args_list), 1)

            # The keystone auth should not be called on another stat
            mock_get_ks.reset_mock()
            s = Path(self.test_container).stat()
            self.assertEquals(s['Account'], 'AUTH_swft_test')
            self.assertEquals(len(mock_get_ks.call_args_list), 0)

            # Set the auth cache to something bad. The auth keystone should
            # be called twice on another stat. It's first called by the swiftclient
            # when retrying auth (with the bad token) and then called by us without
            # a token after the swiftclient raises an authorization error.
            mock_get_ks.reset_mock()
            swift._cached_auth_token_map['AUTH_swft_test']['os_auth_token'] = 'bad_auth'
            s = Path(self.test_container).stat()
            self.assertEquals(s['Account'], 'AUTH_swft_test')
            self.assertEquals(len(mock_get_ks.call_args_list), 2)
            # Note that the auth_token is passed into the keystone client but then popped
            # from the kwargs. Assert that an auth token is no longer part of the retry calls
            self.assertTrue('auth_token' not in mock_get_ks.call_args_list[0][0][3])
            self.assertTrue('auth_token' not in mock_get_ks.call_args_list[1][0][3])

            # Now make the auth always be invalid and verify that an auth error is thrown
            # This also tests that keystone auth errors are propagated as swift
            # AuthenticationErrors
            mock_get_ks.reset_mock()
            swift._clear_cached_auth_credentials()
            with mock.patch('keystoneclient.v2_0.client.Client') as mock_ks_client:
                from keystoneclient.exceptions import Unauthorized
                mock_ks_client.side_effect = Unauthorized
                with self.assertRaises(swift.AuthenticationError):
                    Path(self.test_container).stat()

                # Verify that getting the auth was called two more times because of retry
                # logic
                self.assertEquals(len(mock_get_ks.call_args_list), 2)

    def test_copy_to_from_container(self):
        num_test_objs = 5
        min_obj_size = 100
        with NamedTemporaryDirectory(change_dir=True) as tmp_d:
            self.create_dataset(tmp_d, num_test_objs, min_obj_size)
            for which_obj in self.get_dataset_obj_names(num_test_objs):
                obj_path = storage_utils.join(self.test_container, '%s.txt' % which_obj)
                storage_utils.copy(which_obj, obj_path)
                storage_utils.copy(obj_path, 'copied_file')
                self.assertCorrectObjectContents('copied_file', which_obj, min_obj_size)

    def test_static_large_obj_copy_and_segment_container(self):
        with NamedTemporaryDirectory(change_dir=True) as tmp_d:
            segment_size = 1048576
            obj_size = segment_size * 4 + 100
            self.create_dataset(tmp_d, 1, obj_size)
            obj_path = storage_utils.join(tmp_d,
                                          self.get_dataset_obj_names(1)[0])
            obj_path.copy(self.test_container / 'large_object.txt', swift_retry_options={
                'segment_size': segment_size
            })

            # Verify there is a segment container and that it can be ignored when listing a dir
            segment_container = Path(self.test_container.parent) / ('.segments_%s' % self.test_container.name)  # nopep8
            containers = Path(self.test_container.parent).listdir(ignore_segment_containers=False)
            self.assertTrue(segment_container in containers)
            self.assertTrue(self.test_container in containers)
            containers = Path(self.test_container.parent).listdir(ignore_segment_containers=True)
            self.assertFalse(segment_container in containers)
            self.assertTrue(self.test_container in containers)

            # Verify there are five segments
            objs = set(segment_container.list(condition=lambda results: len(results) == 5))
            self.assertEquals(len(objs), 5)

            # Copy back the large object and verify its contents
            obj_path = Path(tmp_d) / 'large_object.txt'
            Path(self.test_container / 'large_object.txt').copy(obj_path)
            self.assertCorrectObjectContents(obj_path, self.get_dataset_obj_names(1)[0], obj_size)

    def test_hidden_file_nested_dir_copytree(self):
        test_swift_dir = Path(self.test_container) / 'test'
        with NamedTemporaryDirectory(change_dir=True):
            builtins.open('.hidden_file', 'w').close()
            os.symlink('.hidden_file', 'symlink')
            os.mkdir('.hidden_dir')
            os.mkdir('.hidden_dir/nested')
            builtins.open('.hidden_dir/nested/file1', 'w').close()
            builtins.open('.hidden_dir/nested/file2', 'w').close()
            Path('.').copytree(test_swift_dir)

        with NamedTemporaryDirectory(change_dir=True):
            test_swift_dir.copytree('test', swift_download_options={
                'condition': lambda results: len(results) == 4
            })
            self.assertTrue(Path('test/.hidden_file').isfile())
            self.assertTrue(Path('test/symlink').isfile())
            self.assertTrue(Path('test/.hidden_dir').isdir())
            self.assertTrue(Path('test/.hidden_dir/nested').isdir())
            self.assertTrue(Path('test/.hidden_dir/nested/file1').isfile())
            self.assertTrue(Path('test/.hidden_dir/nested/file2').isfile())

    def test_condition_failures(self):
        num_test_objs = 20
        test_obj_size = 100
        test_dir = self.test_container / 'test'
        with NamedTemporaryDirectory(change_dir=True) as tmp_d:
            self.create_dataset(tmp_d, num_test_objs, test_obj_size)
            Path('.').copytree(test_dir)

        # Verify a ConditionNotMet exception is thrown when attempting to list
        # a file that hasn't been uploaded
        expected_objs = {
            test_dir / which_obj
            for which_obj in self.get_dataset_obj_names(num_test_objs + 1)
        }

        with mock.patch('time.sleep') as mock_sleep:
            with self.assertRaises(swift.ConditionNotMetError):
                test_dir.list(condition=lambda results: expected_objs == set(results))
            self.assertTrue(swift.num_retries > 0)
            self.assertEquals(len(mock_sleep.call_args_list), swift.num_retries)

        # Verify that the condition passes when excluding the non-extant file
        expected_objs = {
            test_dir / which_obj
            for which_obj in self.get_dataset_obj_names(num_test_objs)
        }
        objs = test_dir.list(condition=lambda results: expected_objs == set(results))
        self.assertEquals(expected_objs, set(objs))

    def test_list_glob(self):
        num_test_objs = 20
        test_obj_size = 100
        test_dir = self.test_container / 'test'
        with NamedTemporaryDirectory(change_dir=True) as tmp_d:
            self.create_dataset(tmp_d, num_test_objs, test_obj_size)
            Path('.').copytree(test_dir)

        objs = set(test_dir.list(condition=lambda results: len(results) == num_test_objs))
        expected_objs = {
            test_dir / obj_name
            for obj_name in self.get_dataset_obj_names(num_test_objs)
        }
        self.assertEquals(len(objs), num_test_objs)
        self.assertEquals(objs, expected_objs)

        expected_glob = {
            test_dir / obj_name
            for obj_name in self.get_dataset_obj_names(num_test_objs) if obj_name.startswith('1')
        }
        self.assertTrue(len(expected_glob) > 1)
        globbed_objs = set(
            test_dir.glob('1*', condition=lambda results: len(results) == len(expected_glob)))
        self.assertEquals(globbed_objs, expected_glob)

    def test_copytree_to_from_container(self):
        num_test_objs = 10
        test_obj_size = 100
        with NamedTemporaryDirectory(change_dir=True) as tmp_d:
            self.create_dataset(tmp_d, num_test_objs, test_obj_size)
            storage_utils.copytree(
                '.',
                storage_utils.join(self.test_container, 'test'))

        with NamedTemporaryDirectory(change_dir=True) as tmp_d:
            Path(self.test_container / 'test').copytree('test', swift_download_options={
                'condition': lambda results: len(results) == num_test_objs
            })

            # Verify contents of all downloaded test objects
            for which_obj in self.get_dataset_obj_names(num_test_objs):
                obj_path = Path('test') / which_obj
                self.assertCorrectObjectContents(obj_path, which_obj, test_obj_size)

    def test_rmtree(self):
        with NamedTemporaryDirectory(change_dir=True) as tmp_d:
            # Make a couple empty test files and nested files
            tmp_d = Path(tmp_d)
            os.mkdir(tmp_d / 'my_dir')
            open(tmp_d / 'my_dir' / 'dir_file1', 'w').close()
            open(tmp_d / 'my_dir' / 'dir_file2', 'w').close()
            open(tmp_d / 'base_file1', 'w').close()
            open(tmp_d / 'base_file2', 'w').close()

            storage_utils.copytree(
                '.',
                self.test_container,
                swift_upload_options={
                    'use_manifest': True
                })

            swift_dir = self.test_container / 'my_dir'
            self.assertEquals(len(swift_dir.list()), 2)
            swift_dir.rmtree()
            self.assertEquals(len(swift_dir.list()), 0)

            base_contents = self.test_container.list()
            self.assertTrue((self.test_container / 'base_file1') in base_contents)
            self.assertTrue((self.test_container / 'base_file1') in base_contents)

            self.test_container.rmtree()

            with self.assertRaises(swift.NotFoundError):
                self.test_container.list()

    def test_copytree_to_from_container_w_manifest(self):
        num_test_objs = 10
        test_obj_size = 100
        swift_dir = storage_utils.join(self.test_container, 'test')
        with NamedTemporaryDirectory(change_dir=True) as tmp_d:
            self.create_dataset(tmp_d, num_test_objs, test_obj_size)
            # Make a nested file and an empty directory for testing purposes
            tmp_d = Path(tmp_d)
            os.mkdir(tmp_d / 'my_dir')
            open(tmp_d / 'my_dir' / 'empty_file', 'w').close()
            os.mkdir(tmp_d / 'my_dir' / 'empty_dir')

            storage_utils.copytree(
                '.',
                swift_dir,
                swift_upload_options={
                    'use_manifest': True
                })

            # Validate the contents of the manifest file
            manifest_contents = swift._get_data_manifest_contents(swift_dir)
            expected_contents = self.get_dataset_obj_names(num_test_objs)
            expected_contents.extend(['my_dir/empty_file',
                                      'my_dir/empty_dir',
                                      swift.DATA_MANIFEST_FILE_NAME])
            expected_contents = [Path('test') / c for c in expected_contents]
            self.assertEquals(set(manifest_contents), set(expected_contents))

        with NamedTemporaryDirectory(change_dir=True) as tmp_d:
            # Download the results successfully
            Path(self.test_container / 'test').copytree(
                'test',
                swift_download_options={
                    'use_manifest': True
                })

            # Now delete one of the objects from swift. A second download
            # will fail with a condition error
            Path(self.test_container / 'test' / 'my_dir' / 'empty_dir').remove()
            with self.assertRaises(swift.ConditionNotMetError):
                Path(self.test_container / 'test').copytree(
                    'test',
                    swift_download_options={
                        'use_manifest': True,
                        'num_retries': 0
                    })

    def test_is_methods(self):
        container = self.test_container
        container = self.test_container
        file_with_prefix = storage_utils.join(container, 'analysis.txt')

        # ensure container is crated but empty
        sentinel = storage_utils.join(container, 'sentinel')
        with storage_utils.open(sentinel, 'w') as fp:
            fp.write('blah')
        storage_utils.remove(sentinel)
        self.assertTrue(storage_utils.isdir(container))
        self.assertFalse(storage_utils.isfile(container))
        self.assertTrue(storage_utils.exists(container))
        self.assertFalse(storage_utils.listdir(container))

        folder = storage_utils.join(container, 'analysis')
        subfolder = storage_utils.join(container, 'analysis', 'alignments')
        file_in_folder = storage_utils.join(container, 'analysis', 'alignments',
                                            'bam.bam')
        self.assertFalse(storage_utils.exists(file_in_folder))
        self.assertFalse(storage_utils.isdir(folder))
        self.assertFalse(storage_utils.isdir(folder + '/'))
        with storage_utils.open(file_with_prefix, 'w') as fp:
            fp.write('data\n')
        self.assertFalse(storage_utils.isdir(folder))
        self.assertTrue(storage_utils.isfile(file_with_prefix))

        with storage_utils.open(file_in_folder, 'w') as fp:
            fp.write('blah.txt\n')

        self.assertTrue(storage_utils.isdir(folder))
        self.assertFalse(storage_utils.isfile(folder))
        self.assertTrue(storage_utils.isdir(subfolder))

    def test_metadata_pulling(self):
        file_in_folder = storage_utils.join(self.test_container,
                                            'somefile.svg')
        with storage_utils.open(file_in_folder, 'w') as fp:
            fp.write('12345\n')

        self.assertEqual(storage_utils.getsize(file_in_folder), 6)
        stat_data = storage_utils.Path(file_in_folder).stat()
        self.assertIn('Content-Type', stat_data)
        self.assertEqual(stat_data['Content-Type'], 'image/svg+xml')

    def test_gzip_on_remote(self):
        local_gzip = os.path.join(os.path.dirname(__file__),
                                  'file_data/s_3_2126.bcl.gz')
        remote_gzip = storage_utils.join(self.test_container,
                                         storage_utils.basename(local_gzip))
        storage_utils.copy(local_gzip, remote_gzip)
        with storage_utils.open(remote_gzip) as fp:
            with gzip.GzipFile(fileobj=fp) as remote_gzip_fp:
                with gzip.open(local_gzip) as local_gzip_fp:
                    assert_same_data(remote_gzip_fp, local_gzip_fp)