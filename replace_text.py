import sys
import shutil
import tempfile
from pathlib import Path

from sticker_gen import generate_sticker


def process(input_dir: Path, new_text: str):
    if not input_dir.exists():
        print(f"❌ Папка не найдена: {input_dir}")
        return

    files = sorted(input_dir.glob("*.tgs"))
    if not files:
        print(f"⚠️ В папке нет .tgs: {input_dir}")
        return

    total = len(files)
    ok = 0
    failed = 0
    errors = []

    print(f"📁 Папка: {input_dir}")
    print(f"✏️ Новый текст: {new_text!r}")
    print(f"📦 Файлов: {total}")
    print("─" * 60)

    for i, f in enumerate(files, 1):
        try:
            with tempfile.NamedTemporaryFile(suffix=".tgs", delete=False) as tmp:
                tmp_path = tmp.name

            generate_sticker(new_text, str(f), tmp_path)
            shutil.move(tmp_path, str(f))

            ok += 1
            print(f"[{i}/{total}] ✅ {f.name}")
        except ValueError as e:
            failed += 1
            errors.append((f.name, str(e)))
            print(f"[{i}/{total}] ⚠️ {f.name}: {e}")
        except Exception as e:
            failed += 1
            errors.append((f.name, str(e)))
            print(f"[{i}/{total}] ❌ {f.name}: {e}")

    print("─" * 60)
    print(f"✅ Успешно: {ok}")
    print(f"⚠️ Ошибок: {failed}")
    if errors:
        print("\n❌ Проблемные файлы:")
        for name, err in errors[:20]:
            print(f"  {name}: {err}")


def main():
    if len(sys.argv) < 3:
        print("Использование: py replace_text.py <папка> <текст>")
        print("Пример: py replace_text.py templates/shared/main3 text")
        sys.exit(1)

    process(Path(sys.argv[1]), sys.argv[2])


if __name__ == "__main__":
    main()