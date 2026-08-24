import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm


def process_single_file(old_path, root, name, source_base, target_base):
    """Sanitize one file name and copy it into the target tree."""
    base, ext = os.path.splitext(name)

    # Replace punctuation with underscores while preserving letters, digits,
    # underscores, and CJK characters.
    new_base = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', base)

    # Remove a sanitized parent directory name duplicated in the file name.
    parent_dir_name = os.path.basename(root)
    if parent_dir_name:
        # Apply the same normalization before comparing the parent name.
        cleaned_parent = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', parent_dir_name)
        cleaned_parent = re.sub(r'_+', '_', cleaned_parent).strip('_')
        if cleaned_parent and cleaned_parent in new_base:
            new_base = new_base.replace(cleaned_parent, '')

    # Collapse repeated underscores and trim them from both ends.
    new_base = re.sub(r'_+', '_', new_base)
    new_base = new_base.strip('_')

    new_name = new_base + ext

    # Preserve the source directory structure below the target root.
    rel_path = os.path.relpath(root, source_base)
    if rel_path == '.':
        new_root = target_base
    else:
        new_root = os.path.join(target_base, rel_path)

    # Ensure the destination directory exists.
    os.makedirs(new_root, exist_ok=True)

    new_path = os.path.join(new_root, new_name)

    try:
        shutil.copy2(old_path, new_path)
        return True, f"Copied: {name} -> {new_name}"
    except Exception as e:
        return False, f"Error copying {name}: {e}"


def sanitize_and_copy(source_path, target_path, max_workers=16):
    tasks = []

    # Collect every file that needs processing.
    for root, dirs, files in os.walk(source_path, topdown=False):
        for name in files:
            old_path = os.path.join(root, name)
            tasks.append((old_path, root, name, source_path, target_path))

    total_files = len(tasks)
    if total_files == 0:
        print("未找到任何文件。")
        return

    # Copy files in a thread pool because the workload is I/O-bound.
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_single_file, *task): task
            for task in tasks
        }

        # Render aggregate progress with tqdm.
        for future in tqdm(as_completed(futures), total=total_files, desc="复制并清理文件"):
            success, msg = future.result()
            if not success:
                tqdm.write(msg)


if __name__ == '__main__':
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Source directory: ./data/media
    MEDIA_DIR = os.path.abspath(os.path.join(BASE_DIR, "data", "media"))
    # Destination directory: ./data/clean
    TARGET_DIR = os.path.abspath(os.path.join(BASE_DIR, "data", "clean"))

    if os.path.isdir(MEDIA_DIR):
        print(f"源目录: {MEDIA_DIR}")
        print(f"输出目录: {TARGET_DIR}")
        sanitize_and_copy(MEDIA_DIR, TARGET_DIR, max_workers=16)
        print("处理完成！")
    else:
        print("输入的源路径无效或不是目录。")
