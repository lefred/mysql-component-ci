import importlib.machinery
import importlib.util
import json
import textwrap
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('helper', str(ROOT / 'scripts/build-component'))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


class SDKTests(unittest.TestCase):
    def test_detection(self):
        for help_text in ('... component_banquise_agent\n... all\n', 'component_banquise_agent: phony\nall: phony\n'):
            self.assertEqual(helper.detect('MYSQL_ADD_COMPONENT(\n BANQUISE_AGENT x.cc)', help_text), 'component_banquise_agent')
        self.assertEqual(helper.detect('MYSQL_ADD_COMPONENT(vmstat x.cc)', '... component_vmstat'), 'component_vmstat')
        self.assertEqual(helper.detect('set(foo bar)', '... custom', 'custom'), 'custom')
        for code, target in [('MYSQL_ADD_COMPONENT(nope x.cc)', ''),
                             ('MYSQL_ADD_COMPONENT(a a.cc) MYSQL_ADD_COMPONENT(b b.cc)', ''),
                             ('', 'all'), ('', 'minbuild'), ('', 'component_all'), ('', 'component_mysql_server')]:
            with self.assertRaises(RuntimeError):
                helper.detect(code, '... all\n... minbuild\n... component_all\n... component_mysql_server', target)

    def test_workflow_matrix(self):
        workflow = (ROOT / '.github/workflows/build-component.yml').read_text()
        script = textwrap.dedent(workflow.split("python3 - <<'PYTHON'\n", 1)[1].split('          PYTHON', 1)[0])
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            env = dict(os.environ, VERSIONS='8.0.42\n8.4.6\n8.4.6',
                       SDK_IMAGE='quay.io/example/sdk', SDK_IMAGES='{"8.4.6":"quay.io/example/sdk@sha256:abc"}',
                       GITHUB_OUTPUT=str(output))
            subprocess.run(['python3', '-c', script], env=env, check=True)
            matrix = json.loads(output.read_text().split('=', 1)[1])['include']
            self.assertEqual(len(matrix), 2)
            self.assertEqual(matrix[0]['image'], 'quay.io/example/sdk:8.0.42')
            self.assertEqual(matrix[1]['image'], 'quay.io/example/sdk@sha256:abc')
            for invalid in ('8.0.x', 'main', '', '[8.4]', '8.4.6;echo nope'):
                env['VERSIONS'] = invalid
                result = subprocess.run(['python3', '-c', script], env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0, invalid)

    def test_real_cmake_component_only_and_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            server = base / 'server'
            component = base / 'source'
            component.mkdir()
            (server / 'components').mkdir(parents=True)
            (component / 'CMakeLists.txt').write_text('MYSQL_ADD_COMPONENT(BANQUISE_AGENT component.c)\n')
            (component / 'component.c').write_text('int component_function(void) { return 42; }\n')
            for doc in ('README.md', 'LICENSE'):
                (component / doc).write_text('fixture documentation\n')
            (server / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.10)
project(fixture C)
function(MYSQL_ADD_COMPONENT name source)
  string(TOLOWER "${name}" target)
  set(target "component_${target}")
  add_library(${target} MODULE ${source})
  set_target_properties(${target} PROPERTIES PREFIX "")
endfunction()
add_custom_target(server_forbidden ALL COMMAND ${CMAKE_COMMAND} -E false)
if(EXISTS "${CMAKE_SOURCE_DIR}/components/source/CMakeLists.txt")
  add_subdirectory(components/source)
endif()
''')
            output = base / 'output'
            env = dict(os.environ, MYSQL_SOURCE=str(server), COMPONENT_SOURCE=str(component),
                       GITHUB_OUTPUT=str(output), CCACHE_DIR=str(base / 'ccache'))
            subprocess.run(['cmake', '-S', str(server), '-B', str(server / 'build')],
                           env=env, check=True, stdout=subprocess.DEVNULL)
            subprocess.run([str(ROOT / 'scripts/build-component'), 'https://example.org/source.git'], env=env, check=True)
            library = dict(line.split('=', 1) for line in output.read_text().splitlines())['path']
            env.update(GITHUB_WORKSPACE=str(base), COMPONENT_NAME='banquise_agent', COMPONENT_LIBRARY='component_banquise_agent.so',
                       COMPONENT_PATH=library, PACKAGE_VERSION='v1.0', MYSQL_VERSION='8.4.6')
            subprocess.run([str(ROOT / 'scripts/package-component')], env=env, check=True)
            archive = next((base / 'dist').glob('*.tar.gz'))
            with tarfile.open(archive) as tar:
                paths = tar.getnames()
                for suffix in ('lib/mysql/plugin/component_banquise_agent.so', 'share/doc/banquise_agent/README.md',
                               'share/doc/banquise_agent/LICENSE', 'INSTALL.txt'):
                    self.assertTrue(any(path.endswith('/' + suffix) for path in paths), suffix)
                install = tar.extractfile(next(p for p in paths if p.endswith('/INSTALL.txt'))).read().decode()
                self.assertIn("INSTALL COMPONENT 'file://component_banquise_agent';", install)
                self.assertIn('SELECT @@plugin_dir;', install)
            subprocess.run(['sha256sum', '--check', archive.name + '.sha256'], cwd=archive.parent, check=True)

            # Exercise the local URL mode, including an actual fast-forward update.
            def git(*args):
                return subprocess.run(['git', '-C', str(component)] + list(args),
                                      check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('add', '.')
            git('commit', '-m', 'initial')
            (server / 'components/source').unlink()
            env.pop('COMPONENT_SOURCE')
            subprocess.run([str(ROOT / 'scripts/build-component'), str(component)], env=env, check=True)
            (component / 'component.c').write_text('int component_function(void) { return 43; }\n')
            git('add', '.')
            git('commit', '-m', 'update')
            subprocess.run([str(ROOT / 'scripts/build-component'), str(component)], env=env, check=True)
            head = subprocess.check_output(['git', '-C', str(server / 'components/source'), 'rev-parse', 'HEAD'], text=True).strip()
            self.assertEqual(head, git('rev-parse', 'HEAD'))


if __name__ == '__main__':
    unittest.main()
