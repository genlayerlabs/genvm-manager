import importlib.util
import sys
from pathlib import Path

_path = Path(__file__).resolve().parents[3] / 'docs' / 'website' / 'catalog.py'
_spec = importlib.util.spec_from_file_location('catalog', _path)
catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catalog)


def test_catalog_skips_shared_assets(tmp_path, monkeypatch):
	for path in ('manager', 'runner/cpython/abc', 'runner/_static/digest'):
		(tmp_path / path).mkdir(parents=True)
		(tmp_path / path / 'index.html').write_text('')
	monkeypatch.setattr(sys, 'argv', ['catalog', str(tmp_path)])
	assert catalog.main() == 0
	page = (tmp_path / 'index.html').read_text()
	assert 'href="manager/index.html"' in page
	assert 'runner/cpython/abc/index.html' in page
	assert '_static' not in page
