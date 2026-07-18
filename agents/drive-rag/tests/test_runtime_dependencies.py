import importlib
import shutil


def test_production_python_dependencies_are_importable():
    modules = ("chromadb", "fastembed", "fitz", "docx", "pptx", "openpyxl", "bs4", "PIL", "pytesseract", "tokenizers")
    for module in modules:
        assert importlib.import_module(module)


def test_tesseract_is_installed():
    assert shutil.which("tesseract")
