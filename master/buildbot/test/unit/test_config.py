# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

import re
import os
import textwrap
import mock
from zope.interface import implements
from twisted.trial import unittest
from twisted.application import service
from twisted.internet import defer
from buildbot import config, buildslave, interfaces
from buildbot.process import properties
from buildbot.test.util import dirs, compat
from buildbot.changes import base as changes_base
from buildbot.schedulers import base as schedulers_base
from buildbot.status import base as status_base

global_defaults = dict(
    title='Buildbot',
    titleURL='http://buildbot.net',
    buildbotURL='http://localhost:8080/',
    changeHorizon=None,
    eventHorizon=50,
    logHorizon=None,
    buildHorizon=None,
    logCompressionLimit=4096,
    logCompressionMethod='bz2',
    logMaxTailSize=None,
    logMaxSize=None,
    properties=properties.Properties(),
    mergeRequests=None,
    prioritizeBuilders=None,
    slavePortnum=None,
    multiMaster=False,
    debugPassword=None,
    manhole=None,
)


class FakeChangeSource(changes_base.ChangeSource):
    pass


class FakeStatusReceiver(status_base.StatusReceiver):
    pass


class FakeScheduler(object):
    implements(interfaces.IScheduler)
    def __init__(self, name):
        self.name = name

class FakeBuilder(object):

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class ConfigErrorsMixin(object):

    def assertConfigError(self, errors, substr_or_re):
        if len(errors.errors) > 1:
            self.fail("too many errors: %s" % (errors.errors,))
        elif len(errors.errors) < 1:
            self.fail("expected error did not occur")
        elif isinstance(substr_or_re, str):
            if substr_or_re not in errors.errors[0]:
                self.fail("non-matching error: %s" % (errors.errors,))
        else:
            if not substr_or_re.search(errors.errors[0]):
                self.fail("non-matching error: %s" % (errors.errors,))

    def assertRaisesConfigError(self, substr_or_re, fn):
        try:
            fn()
        except config.ConfigErrors, e:
            self.assertConfigError(e, substr_or_re)
        else:
            self.fail("ConfigErrors not raised")

    def assertNoConfigErrors(self, errors):
        self.assertEqual(errors.errors, [])


class ConfigErrors(unittest.TestCase):

    def test_constr(self):
        ex = config.ConfigErrors(['a', 'b'])
        self.assertEqual(ex.errors, ['a', 'b'])

    def test_addError(self):
        ex = config.ConfigErrors(['a'])
        ex.addError('c')
        self.assertEqual(ex.errors, ['a', 'c'])

    def test_nonempty(self):
        empty = config.ConfigErrors()
        full = config.ConfigErrors(['a'])
        self.failUnless(not empty)
        self.failIf(not full)


class MasterConfig(ConfigErrorsMixin, dirs.DirsMixin, unittest.TestCase):

    def setUp(self):
        self.basedir = os.path.abspath('basedir')
        self.filename = 'test.cfg'
        return self.setUpDirs('basedir')

    def tearDown(self):
        return self.tearDownDirs()

    # utils

    def patch_load_helpers(self):
        # patch out all of the "helpers" for laodConfig with null functions
        for n in dir(config.MasterConfig):
            if n.startswith('load_'):
                typ = 'loader'
            elif n.startswith('check_'):
                typ = 'checker'
            else:
                continue

            v = getattr(config.MasterConfig, n)
            if callable(v):
                if typ == 'loader':
                    self.patch(config.MasterConfig, n,
                        mock.Mock(side_effect=
                            lambda filename, config_dict, errors : None))
                else:
                    self.patch(config.MasterConfig, n,
                        mock.Mock(side_effect=
                            lambda errors : None))


    def install_config_file(self, config_file, other_files={}):
        config_file = textwrap.dedent(config_file)
        open(os.path.join(self.basedir, self.filename), "w").write(config_file)
        for file, contents in other_files.items():
            open(file, "w").write(contents)


    # tests

    def test_defaults(self):
        cfg = config.MasterConfig()
        expected = dict(
            #validation,
            db=dict(
                db_url='sqlite:///state.sqlite',
                db_poll_interval=None),
            metrics = None,
            caches = dict(Changes=10, Builds=15),
            schedulers = {},
            builders = [],
            slaves = [],
            change_sources = [],
            status = [],
            user_managers = [],
            )
        expected.update(global_defaults)
        got = dict([
            (attr, getattr(cfg, attr))
            for attr, exp in expected.iteritems() ])
        self.assertEqual(got, expected)

    def test_defaults_validation(self):
        # re's aren't comparable, but we can make sure the keys match
        cfg = config.MasterConfig()
        self.assertEqual(sorted(cfg.validation.keys()),
            sorted([
                'branch', 'revision', 'property_name', 'property_value',
            ]))

    def test_loadConfig_missing_file(self):
        self.assertRaisesConfigError(
            re.compile("configuration file .* does not exist"),
            lambda : config.MasterConfig.loadConfig(
                self.basedir, self.filename))

    def test_loadConfig_missing_basedir(self):
        self.assertRaisesConfigError(
            re.compile("basedir .* does not exist"),
            lambda : config.MasterConfig.loadConfig(
                os.path.join(self.basedir, 'NO'), 'test.cfg'))

    @compat.usesFlushLoggedErrors
    def test_loadConfig_parse_error(self):
        self.install_config_file('def x:\nbar')
        self.assertRaisesConfigError(
            re.compile("error while parsing.*traceback in logfile"),
            lambda : config.MasterConfig.loadConfig(
                self.basedir, self.filename))
        self.assertEqual(len(self.flushLoggedErrors(SyntaxError)), 1)

    def test_loadConfig_eval_ConfigErrors(self):
        self.install_config_file("""\
                from buildbot import config
                raise config.ConfigErrors(['oh noes!'])""")
        self.assertRaisesConfigError("oh noes",
            lambda : config.MasterConfig.loadConfig(
                self.basedir, self.filename))

    def test_loadConfig_no_BuildmasterConfig(self):
        self.install_config_file('x=10')
        self.assertRaisesConfigError("does not define 'BuildmasterConfig'",
            lambda : config.MasterConfig.loadConfig(
                self.basedir, self.filename))

    def test_loadConfig_unknown_key(self):
        self.patch_load_helpers()
        self.install_config_file("""\
                BuildmasterConfig = dict(foo=10)
                """)
        self.assertRaisesConfigError("Unknown BuildmasterConfig key foo",
            lambda : config.MasterConfig.loadConfig(
                self.basedir, self.filename))

    def test_loadConfig_unknown_keys(self):
        self.patch_load_helpers()
        self.install_config_file("""\
                BuildmasterConfig = dict(foo=10, bar=20)
                """)
        self.assertRaisesConfigError("Unknown BuildmasterConfig keys bar, foo",
            lambda : config.MasterConfig.loadConfig(
                self.basedir, self.filename))

    def test_loadConfig_success(self):
        self.patch_load_helpers()
        self.install_config_file("""\
                BuildmasterConfig = dict()
                """)
        rv = config.MasterConfig.loadConfig(
            self.basedir, self.filename)
        self.assertIsInstance(rv, config.MasterConfig)

        # make sure all of the loaders and checkers are called
        self.failUnless(rv.load_global.called)
        self.failUnless(rv.load_validation.called)
        self.failUnless(rv.load_db.called)
        self.failUnless(rv.load_metrics.called)
        self.failUnless(rv.load_caches.called)
        self.failUnless(rv.load_schedulers.called)
        self.failUnless(rv.load_builders.called)
        self.failUnless(rv.load_slaves.called)
        self.failUnless(rv.load_change_sources.called)
        self.failUnless(rv.load_status.called)
        self.failUnless(rv.load_user_managers.called)

        self.failUnless(rv.check_single_master.called)
        self.failUnless(rv.check_schedulers.called)
        self.failUnless(rv.check_locks.called)
        self.failUnless(rv.check_builders.called)
        self.failUnless(rv.check_status.called)
        self.failUnless(rv.check_horizons.called)
        self.failUnless(rv.check_slavePortnum.called)

    def test_loadConfig_with_local_import(self):
        self.patch_load_helpers()
        self.install_config_file("""\
                from subsidiary_module import x
                BuildmasterConfig = dict()
                """,
                {'basedir/subsidiary_module.py' : "x = 10"})
        rv = config.MasterConfig.loadConfig(
            self.basedir, self.filename)
        self.assertIsInstance(rv, config.MasterConfig)


class MasterConfig_loaders(ConfigErrorsMixin, unittest.TestCase):

    filename = 'test.cfg'

    def setUp(self):
        self.cfg = config.MasterConfig()
        self.errors = config.ConfigErrors()

    # utils

    def assertResults(self, **expected):
        self.failIf(self.errors, self.errors.errors)
        got = dict([
            (attr, getattr(self.cfg, attr))
            for attr, exp in expected.iteritems() ])
        self.assertEqual(got, expected)

    # tests

    def test_load_global_defaults(self):
        self.cfg.load_global(self.filename, {}, self.errors)
        self.assertResults(**global_defaults)

    def test_load_global_string_param_not_string(self):
        self.cfg.load_global(self.filename,
                dict(title=10), self.errors)
        self.assertConfigError(self.errors, 'must be a string')

    def test_load_global_int_param_not_int(self):
        self.cfg.load_global(self.filename,
                dict(changeHorizon='yes'), self.errors)
        self.assertConfigError(self.errors, 'must be an int')

    def do_test_load_global(self, config_dict, **expected):
        self.cfg.load_global(self.filename, config_dict, self.errors)
        self.assertResults(**expected)

    def test_load_global_title(self):
        self.do_test_load_global(dict(title='hi'), title='hi')

    def test_load_global_projectURL(self):
        self.do_test_load_global(dict(projectName='hey'), title='hey')

    def test_load_global_titleURL(self):
        self.do_test_load_global(dict(titleURL='hi'), titleURL='hi')

    def test_load_global_buildbotURL(self):
        self.do_test_load_global(dict(buildbotURL='hey'), buildbotURL='hey')

    def test_load_global_changeHorizon(self):
        self.do_test_load_global(dict(changeHorizon=10), changeHorizon=10)

    def test_load_global_changeHorizon_none(self):
        self.do_test_load_global(dict(changeHorizon=None), changeHorizon=None)

    def test_load_global_eventHorizon(self):
        self.do_test_load_global(dict(eventHorizon=10), eventHorizon=10)

    def test_load_global_logHorizon(self):
        self.do_test_load_global(dict(logHorizon=10), logHorizon=10)

    def test_load_global_buildHorizon(self):
        self.do_test_load_global(dict(buildHorizon=10), buildHorizon=10)

    def test_load_global_logCompressionLimit(self):
        self.do_test_load_global(dict(logCompressionLimit=10),
                                 logCompressionLimit=10)

    def test_load_global_logCompressionMethod(self):
        self.do_test_load_global(dict(logCompressionMethod='gz'),
                                 logCompressionMethod='gz')

    def test_load_global_logCompressionMethod_invalid(self):
        self.cfg.load_global(self.filename,
                dict(logCompressionMethod='foo'), self.errors)
        self.assertConfigError(self.errors, "must be 'bz2' or 'gz'")

    def test_load_global_logMaxSize(self):
        self.do_test_load_global(dict(logMaxSize=123), logMaxSize=123)

    def test_load_global_logMaxTailSize(self):
        self.do_test_load_global(dict(logMaxTailSize=123), logMaxTailSize=123)

    def test_load_global_properties(self):
        exp = properties.Properties()
        exp.setProperty('x', 10, self.filename)
        self.do_test_load_global(dict(properties=dict(x=10)), properties=exp)

    def test_load_global_properties_invalid(self):
        self.cfg.load_global(self.filename,
                dict(properties='yes'), self.errors)
        self.assertConfigError(self.errors, "must be a dictionary")

    def test_load_global_mergeRequests_bool(self):
        self.do_test_load_global(dict(mergeRequests=False),
                mergeRequests=False)

    def test_load_global_mergeRequests_callable(self):
        callable = lambda : None
        self.do_test_load_global(dict(mergeRequests=callable),
                mergeRequests=callable)

    def test_load_global_mergeRequests_invalid(self):
        self.cfg.load_global(self.filename,
                dict(mergeRequests='yes'), self.errors)
        self.assertConfigError(self.errors,
                "must be a callable, True, or False")

    def test_load_global_prioritizeBuilders_callable(self):
        callable = lambda : None
        self.do_test_load_global(dict(prioritizeBuilders=callable),
                prioritizeBuilders=callable)

    def test_load_global_prioritizeBuilders_invalid(self):
        self.cfg.load_global(self.filename,
                dict(prioritizeBuilders='yes'), self.errors)
        self.assertConfigError(self.errors, "must be a callable")

    def test_load_global_slavePortnum_int(self):
        self.do_test_load_global(dict(slavePortnum=123),
                slavePortnum='tcp:123')

    def test_load_global_slavePortnum_str(self):
        self.do_test_load_global(dict(slavePortnum='udp:123'),
                slavePortnum='udp:123')

    def test_load_global_multiMaster(self):
        self.do_test_load_global(dict(multiMaster=1), multiMaster=1)

    def test_load_global_debugPassword(self):
        self.do_test_load_global(dict(debugPassword='xyz'),
                debugPassword='xyz')

    def test_load_global_manhole(self):
        mh = mock.Mock(name='manhole')
        self.do_test_load_global(dict(manhole=mh), manhole=mh)


    def test_load_validation_defaults(self):
        self.cfg.load_validation(self.filename, {}, self.errors)
        self.assertEqual(sorted(self.cfg.validation.keys()),
            sorted([
                'branch', 'revision', 'property_name', 'property_value',
            ]))

    def test_load_validation_invalid(self):
        self.cfg.load_validation(self.filename,
                dict(validation='plz'), self.errors)
        self.assertConfigError(self.errors, "must be a dictionary")

    def test_load_validation_unk_keys(self):
        self.cfg.load_validation(self.filename,
                dict(validation=dict(users='.*')), self.errors)
        self.assertConfigError(self.errors, "unrecognized validation key(s)")

    def test_load_validation(self):
        r = re.compile('.*')
        self.cfg.load_validation(self.filename,
                dict(validation=dict(branch=r)), self.errors)
        self.assertEqual(self.cfg.validation['branch'], r)
        # check that defaults are still around
        self.assertIn('revision', self.cfg.validation)


    def test_load_db_defaults(self):
        self.cfg.load_db(self.filename, {}, self.errors)
        self.assertResults(
            db=dict(db_url='sqlite:///state.sqlite', db_poll_interval=None))

    def test_load_db_db_url(self):
        self.cfg.load_db(self.filename, dict(db_url='abcd'), self.errors)
        self.assertResults(db=dict(db_url='abcd', db_poll_interval=None))

    def test_load_db_db_poll_interval(self):
        self.cfg.load_db(self.filename, dict(db_poll_interval=2), self.errors)
        self.assertResults(
            db=dict(db_url='sqlite:///state.sqlite', db_poll_interval=2))

    def test_load_db_dict(self):
        self.cfg.load_db(self.filename,
            dict(db=dict(db_url='abcd', db_poll_interval=10)),
            self.errors)
        self.assertResults(db=dict(db_url='abcd', db_poll_interval=10))

    def test_load_db_unk_keys(self):
        self.cfg.load_db(self.filename,
            dict(db=dict(db_url='abcd', db_poll_interval=10, bar='bar')),
            self.errors)
        self.assertConfigError(self.errors, "unrecognized keys in")

    def test_load_db_not_int(self):
        self.cfg.load_db(self.filename,
            dict(db=dict(db_url='abcd', db_poll_interval='ten')),
            self.errors)
        self.assertConfigError(self.errors, "must be an int")


    def test_load_metrics_defaults(self):
        self.cfg.load_metrics(self.filename, {}, self.errors)
        self.assertResults(metrics=None)

    def test_load_metrics_invalid(self):
        self.cfg.load_metrics(self.filename, dict(metrics=13), self.errors)
        self.assertConfigError(self.errors, "must be a dictionary")

    def test_load_metrics(self):
        self.cfg.load_metrics(self.filename,
                dict(metrics=dict(foo=1)),
                self.errors)
        self.assertResults(metrics=dict(foo=1))


    def test_load_caches_defaults(self):
        self.cfg.load_caches(self.filename, {}, self.errors)
        self.assertResults(caches=dict(Changes=10, Builds=15))

    def test_load_caches_invalid(self):
        self.cfg.load_caches(self.filename, dict(caches=13), self.errors)
        self.assertConfigError(self.errors, "must be a dictionary")

    def test_load_caches_buildCacheSize(self):
        self.cfg.load_caches(self.filename,
                dict(buildCacheSize=13),
                self.errors)
        self.assertResults(caches=dict(Builds=13, Changes=10))

    def test_load_caches_buildCacheSize_and_caches(self):
        self.cfg.load_caches(self.filename,
                dict(buildCacheSize=13, caches=dict(builds=11)),
                self.errors)
        self.assertConfigError(self.errors, "cannot specify")

    def test_load_caches_changeCacheSize(self):
        self.cfg.load_caches(self.filename,
                dict(changeCacheSize=13),
                self.errors)
        self.assertResults(caches=dict(Changes=13, Builds=15))

    def test_load_caches_changeCacheSize_and_caches(self):
        self.cfg.load_caches(self.filename,
                dict(changeCacheSize=13, caches=dict(changes=11)),
                self.errors)
        self.assertConfigError(self.errors, "cannot specify")

    def test_load_caches(self):
        self.cfg.load_caches(self.filename,
                dict(caches=dict(foo=1)),
                self.errors)
        self.assertResults(caches=dict(Changes=10, Builds=15, foo=1))


    def test_load_schedulers_defaults(self):
        self.cfg.load_schedulers(self.filename, {}, self.errors)
        self.assertResults(schedulers={})

    def test_load_schedulers_not_list(self):
        self.cfg.load_schedulers(self.filename,
                dict(schedulers=dict()), self.errors)
        self.assertConfigError(self.errors, "must be a list of")

    def test_load_schedulers_not_instance(self):
        self.cfg.load_schedulers(self.filename,
                dict(schedulers=[mock.Mock()]), self.errors)
        self.assertConfigError(self.errors, "must be a list of")

    def test_load_schedulers_dupe(self):
        sch1 = FakeScheduler(name='sch')
        sch2 = FakeScheduler(name='sch')
        self.cfg.load_schedulers(self.filename,
                dict(schedulers=[ sch1, sch2 ]), self.errors)
        self.assertConfigError(self.errors,
                "scheduler name 'sch' used multiple times")

    def test_load_schedulers(self):
        class Sch(schedulers_base.BaseScheduler):
            def __init__(self, name):
                self.name = name
        sch = Sch('sch')
        self.cfg.load_schedulers(self.filename,
                dict(schedulers=[sch]), self.errors)
        self.assertResults(schedulers=dict(sch=sch))


    def test_load_builders_defaults(self):
        self.cfg.load_builders(self.filename, {}, self.errors)
        self.assertResults(builders=[])

    def test_load_builders_not_list(self):
        self.cfg.load_builders(self.filename,
                dict(builders=dict()), self.errors)
        self.assertConfigError(self.errors, "must be a list")

    def test_load_builders_not_instance(self):
        self.cfg.load_builders(self.filename,
                dict(builders=[mock.Mock()]), self.errors)
        self.assertConfigError(self.errors, "must be a list of")

    def test_load_builders(self):
        bldr = config.BuilderConfig(name='x',
                        factory=mock.Mock(), slavename='x')
        self.cfg.load_builders(self.filename,
                dict(builders=[bldr]), self.errors)
        self.assertResults(builders=[bldr])

    def test_load_builders_dict(self):
        bldr = dict(name='x', factory=mock.Mock(), slavename='x')
        self.cfg.load_builders(self.filename,
                dict(builders=[bldr]), self.errors)
        self.assertIsInstance(self.cfg.builders[0], config.BuilderConfig)
        self.assertEqual(self.cfg.builders[0].name, 'x')


    def test_load_slaves_defaults(self):
        self.cfg.load_slaves(self.filename, {}, self.errors)
        self.assertResults(slaves=[])

    def test_load_slaves_not_list(self):
        self.cfg.load_slaves(self.filename,
                dict(slaves=dict()), self.errors)
        self.assertConfigError(self.errors, "must be a list")

    def test_load_slaves_not_instance(self):
        self.cfg.load_slaves(self.filename,
                dict(slaves=[mock.Mock()]), self.errors)
        self.assertConfigError(self.errors, "must be a list of")

    def test_load_slaves_reserved_names(self):
        for name in 'debug', 'change', 'status':
            self.cfg.load_slaves(self.filename,
                    dict(slaves=[buildslave.BuildSlave(name, 'x')]),
                    self.errors)
            self.assertConfigError(self.errors, "is reserved")
            self.errors.errors[:] = [] # clear out the errors

    def test_load_slaves(self):
        sl = buildslave.BuildSlave('foo', 'x')
        self.cfg.load_slaves(self.filename,
                dict(slaves=[sl]), self.errors)
        self.assertResults(slaves=[sl])


    def test_load_change_sources_defaults(self):
        self.cfg.load_change_sources(self.filename, {}, self.errors)
        self.assertResults(change_sources=[])

    def test_load_change_sources_not_instance(self):
        self.cfg.load_change_sources(self.filename,
                dict(change_source=[mock.Mock()]), self.errors)
        self.assertConfigError(self.errors, "must be a list of")

    def test_load_change_sources_single(self):
        chsrc = FakeChangeSource()
        self.cfg.load_change_sources(self.filename,
                dict(change_source=chsrc), self.errors)
        self.assertResults(change_sources=[chsrc])

    def test_load_change_sources_list(self):
        chsrc = FakeChangeSource()
        self.cfg.load_change_sources(self.filename,
                dict(change_source=[chsrc]), self.errors)
        self.assertResults(change_sources=[chsrc])


    def test_load_user_managers_defaults(self):
        self.cfg.load_user_managers(self.filename, {}, self.errors)
        self.assertResults(user_managers=[])

    def test_load_user_managers_not_list(self):
        self.cfg.load_user_managers(self.filename,
                dict(user_managers='foo'),
                self.errors)
        self.assertConfigError(self.errors, "must be a list")

    def test_load_user_managers(self):
        um = mock.Mock()
        self.cfg.load_user_managers(self.filename,
                dict(user_managers=[um]),
                self.errors)
        self.assertResults(user_managers=[um])

class MasterConfig_checkers(ConfigErrorsMixin, unittest.TestCase):

    def setUp(self):
        self.cfg = config.MasterConfig()
        self.errors = config.ConfigErrors()

    # utils

    def setup_basic_attrs(self):
        # set up a basic config for checking; this will be modified below
        sch = mock.Mock()
        sch.name = 'sch'
        sch.listBuilderNames = lambda : [ 'b1', 'b2' ]

        b1 = mock.Mock()
        b1.name = 'b1'

        b2 = mock.Mock()
        b2.name = 'b2'

        self.cfg.schedulers = dict(sch=sch)
        self.cfg.slaves = [ mock.Mock() ]
        self.cfg.builders = [ b1, b2 ]

    def setup_builder_locks(self, builder_lock=None, dup_builder_lock=False,
                                  step_lock=None, dup_step_lock=False):
        def bldr(name):
            b = mock.Mock()
            b.name = name
            b.locks = []
            b.factory.steps = [ ('cls', dict(locks=[])) ]
            return b

        def lock(name):
            l = mock.Mock()
            l.name = name
            return l

        b1, b2 = bldr('b1'), bldr('b2')
        self.cfg.builders = [ b1, b2 ]
        if builder_lock:
            b1.locks.append(lock(builder_lock))
            if dup_builder_lock:
                b2.locks.append(lock(builder_lock))
        if step_lock:
            s1, s2 = b1.factory.steps[0][1], b2.factory.steps[0][1]
            s1['locks'].append(lock(step_lock))
            if dup_step_lock:
                s2['locks'].append(lock(step_lock))

    # tests

    def test_check_single_master_multimaster(self):
        self.cfg.multiMaster = True
        self.cfg.check_single_master(self.errors)
        self.assertNoConfigErrors(self.errors)

    def test_check_single_master_no_builders(self):
        self.setup_basic_attrs()
        self.cfg.builders = [ ]
        self.cfg.check_single_master(self.errors)
        self.assertConfigError(self.errors, "no builders are configured")

    def test_check_single_master_no_slaves(self):
        self.setup_basic_attrs()
        self.cfg.slaves = [ ]
        self.cfg.check_single_master(self.errors)
        self.assertConfigError(self.errors, "no slaves are configured")

    def test_check_single_master_unsch_builder(self):
        self.setup_basic_attrs()
        b3 = mock.Mock()
        b3.name = 'b3'
        self.cfg.builders.append(b3)
        self.cfg.check_single_master(self.errors)
        self.assertConfigError(self.errors, "have no schedulers to drive them")


    def test_check_schedulers_unknown_builder(self):
        self.setup_basic_attrs()
        del self.cfg.builders[1] # remove b2, leaving b1

        self.cfg.check_schedulers(self.errors)
        self.assertConfigError(self.errors, "Unknown builder 'b2'")

    def test_check_schedulers(self):
        self.setup_basic_attrs()
        self.cfg.check_schedulers(self.errors)
        self.assertNoConfigErrors(self.errors)


    def test_check_locks_step_and_builder(self):
        self.setup_builder_locks(builder_lock='l', step_lock='l')
        self.cfg.check_locks(self.errors)
        self.assertConfigError(self.errors, "Two locks share")

    def test_check_locks_dup_builder_lock(self):
        self.setup_builder_locks(builder_lock='l', dup_builder_lock=True)
        self.cfg.check_locks(self.errors)
        self.assertConfigError(self.errors, "Two locks share")

    def test_check_locks_dup_step_lock(self):
        self.setup_builder_locks(step_lock='l', dup_step_lock=True)
        self.cfg.check_locks(self.errors)
        self.assertConfigError(self.errors, "Two locks share")

    def test_check_locks(self):
        self.setup_builder_locks(builder_lock='bl', step_lock='sl')
        self.cfg.check_locks(self.errors)
        self.assertNoConfigErrors(self.errors)

    def test_check_locks_none(self):
        # no locks in the whole config, should be fine
        self.setup_builder_locks()
        self.cfg.check_locks(self.errors)
        self.assertNoConfigErrors(self.errors)


    def test_check_builders_unknown_slave(self):
        sl = mock.Mock()
        sl.slavename = 'xyz'
        self.cfg.slaves = [ sl ]

        b1 = FakeBuilder(slavenames=[ 'xyz', 'abc' ], builddir='x', name='b1')
        self.cfg.builders = [ b1 ]

        self.cfg.check_builders(self.errors)
        self.assertConfigError(self.errors,
                "builder 'b1' uses unknown slaves 'abc'")

    def test_check_builders_duplicate_name(self):
        b1 = FakeBuilder(slavenames=[], name='b1', builddir='1')
        b2 = FakeBuilder(slavenames=[], name='b1', builddir='2')
        self.cfg.builders = [ b1, b2 ]

        self.cfg.check_builders(self.errors)
        self.assertConfigError(self.errors,
                "duplicate builder name 'b1'")

    def test_check_builders_duplicate_builddir(self):
        b1 = FakeBuilder(slavenames=[], name='b1', builddir='dir')
        b2 = FakeBuilder(slavenames=[], name='b2', builddir='dir')
        self.cfg.builders = [ b1, b2 ]

        self.cfg.check_builders(self.errors)
        self.assertConfigError(self.errors,
                "duplicate builder builddir 'dir'")

    def test_check_builders(self):
        sl = mock.Mock()
        sl.slavename = 'a'
        self.cfg.slaves = [ sl ]

        b1 = FakeBuilder(slavenames=[ 'a' ], name='b1', builddir='dir1')
        b2 = FakeBuilder(slavenames=[ 'a' ], name='b2', builddir='dir2')
        self.cfg.builders = [ b1, b2 ]

        self.cfg.check_builders(self.errors)
        self.assertNoConfigErrors(self.errors)


    def test_check_status_fails(self):
        st = FakeStatusReceiver()
        st.checkConfig = lambda status, errors : errors.addError("oh noes")
        self.cfg.status = [ st ]

        self.cfg.check_status(self.errors)

        self.assertConfigError(self.errors, "oh noes")

    def test_check_status(self):
        st = FakeStatusReceiver()
        st.checkConfig = mock.Mock()
        self.cfg.status = [ st ]

        self.cfg.check_status(self.errors)

        self.assertNoConfigErrors(self.errors)
        st.checkConfig.assert_called_once_with(self.cfg.status, self.errors)

    def test_check_horizons(self):
        self.cfg.logHorizon = 100
        self.cfg.buildHorizon = 50
        self.cfg.check_horizons(self.errors)

        self.assertConfigError(self.errors, "logHorizon must be less")

    def test_check_slavePortnum_set(self):
        self.cfg.slavePortnum = 10
        self.cfg.check_slavePortnum(self.errors)
        self.assertNoConfigErrors(self.errors)

    def test_check_slavePortnum_not_set_slaves(self):
        self.cfg.slaves = [ mock.Mock() ]
        self.cfg.check_slavePortnum(self.errors)
        self.assertConfigError(self.errors,
                "slaves are configured, but no slavePortnum is set")

    def test_check_slavePortnum_not_set_debug(self):
        self.cfg.debugPassword = 'ssh'
        self.cfg.check_slavePortnum(self.errors)
        self.assertConfigError(self.errors,
                "debug client is configured, but no slavePortnum is set")


class BuilderConfig(ConfigErrorsMixin, unittest.TestCase):

    factory = mock.Mock()

    # utils

    def assertAttributes(self, cfg, **expected):
        got = dict([
            (attr, getattr(cfg, attr))
            for attr, exp in expected.iteritems() ])
        self.assertEqual(got, expected)

    # tests

    def test_no_name(self):
        self.assertRaisesConfigError(
            "builder's name is required",
            lambda : config.BuilderConfig(
                factory=self.factory, slavenames=['a']))

    def test_reserved_name(self):
        self.assertRaisesConfigError(
            "builder names must not start with an underscore: '_a'",
            lambda : config.BuilderConfig(name='_a',
                factory=self.factory, slavenames=['a']))

    def test_no_factory(self):
        self.assertRaisesConfigError(
            "builder 'a' has no factory",
            lambda : config.BuilderConfig(
                name='a', slavenames=['a']))

    def test_no_slavenames(self):
        self.assertRaisesConfigError(
            "builder 'a': at least one slavename is required",
            lambda : config.BuilderConfig(
                name='a', factory=self.factory))

    def test_bogus_slavenames(self):
        self.assertRaisesConfigError(
            "slavenames must be a list or a string",
            lambda : config.BuilderConfig(
                name='a', slavenames={1:2}, factory=self.factory))

    def test_bogus_slavename(self):
        self.assertRaisesConfigError(
            "slavename must be a string",
            lambda : config.BuilderConfig(
                name='a', slavename=1, factory=self.factory))

    def test_bogus_category(self):
        self.assertRaisesConfigError(
            "category must be a string",
            lambda : config.BuilderConfig(category=13,
                name='a', slavenames=['a'], factory=self.factory))

    def test_defaults(self):
        cfg = config.BuilderConfig(
            name='a b c', slavename='a', factory=self.factory)
        self.assertIdentical(cfg.factory, self.factory)
        self.assertAttributes(cfg,
            name='a b c',
            slavenames=['a'],
            builddir='a_b_c',
            slavebuilddir='a_b_c',
            category='',
            nextSlave=None,
            locks=[],
            env={},
            properties={},
            mergeRequests=None)

    def test_args(self):
        cfg = config.BuilderConfig(
            name='b', slavename='s1', slavenames='s2', builddir='bd',
            slavebuilddir='sbd', factory=self.factory, category='c',
            nextSlave=lambda : 'ns', nextBuild=lambda : 'nb', locks=['l'],
            env=dict(x=10), properties=dict(y=20), mergeRequests='mr')
        self.assertIdentical(cfg.factory, self.factory)
        self.assertAttributes(cfg,
            name='b',
            slavenames=['s2', 's1'],
            builddir='bd',
            slavebuilddir='sbd',
            category='c',
            locks=['l'],
            env={'x':10},
            properties={'y':20},
            mergeRequests='mr')

    def test_getConfigDict(self):
        ns = lambda : 'ns'
        nb = lambda : 'nb'
        cfg = config.BuilderConfig(
            name='b', slavename='s1', slavenames='s2', builddir='bd',
            slavebuilddir='sbd', factory=self.factory, category='c',
            nextSlave=ns, nextBuild=nb, locks=['l'],
            env=dict(x=10), properties=dict(y=20), mergeRequests='mr')
        self.assertEqual(cfg.getConfigDict(), {'builddir': 'bd',
            'category': 'c',
            'env': {'x': 10},
            'factory': self.factory,
            'locks': ['l'],
            'mergeRequests': 'mr',
            'name': 'b',
            'nextBuild': nb,
            'nextSlave': ns,
            'properties': {'y': 20},
            'slavebuilddir': 'sbd',
            'slavenames': ['s2', 's1'],
        })



class FakeService(config.ReconfigurableServiceMixin,
                    service.Service):

    succeed = True

    def reconfigService(self, new_config):
        self.called = True
        d = config.ReconfigurableServiceMixin.reconfigService(self, new_config)
        if not self.succeed:
            @d.addCallback
            def fail(_):
                raise ValueError("oh noes")
        return d



class FakeMultiService(config.ReconfigurableServiceMixin,
                    service.MultiService):

    def reconfigService(self, new_config):
        self.called = True
        d = config.ReconfigurableServiceMixin.reconfigService(self, new_config)
        return d



class ReconfigurableServiceMixin(unittest.TestCase):

    def test_service(self):
        svc = FakeService()
        d = svc.reconfigService(mock.Mock())
        @d.addCallback
        def check(_):
            self.assertTrue(svc.called)
        return d

    @defer.deferredGenerator
    def test_service_failure(self):
        svc = FakeService()
        svc.succeed = False
        try:
            wfd = defer.waitForDeferred(
                svc.reconfigService(mock.Mock()))
            yield wfd
            wfd.getResult()
        except ValueError:
            pass
        else:
            self.fail("should have raised ValueError")

    def test_multiservice(self):
        svc = FakeMultiService()
        ch1 = FakeService()
        ch1.setServiceParent(svc)
        ch2 = FakeMultiService()
        ch2.setServiceParent(svc)
        ch3 = FakeService()
        ch3.setServiceParent(ch2)
        d = svc.reconfigService(mock.Mock())
        @d.addCallback
        def check(_):
            self.assertTrue(svc.called)
            self.assertTrue(ch1.called)
            self.assertTrue(ch2.called)
            self.assertTrue(ch3.called)
        return d

    @compat.usesFlushLoggedErrors
    @defer.deferredGenerator
    def test_multiservice_nested_failure(self):
        svc = FakeMultiService()
        ch1 = FakeService()
        ch1.setServiceParent(svc)
        ch1.succeed = False
        try:
            wfd = defer.waitForDeferred(
                svc.reconfigService(mock.Mock()))
            yield wfd
            wfd.getResult()
        except ValueError:
            pass
        else:
            self.fail("should have raised ValueError")
