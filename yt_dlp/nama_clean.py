import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm


def process_single_file(old_path, root, name, source_base, target_base):
    """处理单个文件的路径重命名与复制"""
    base, ext = os.path.splitext(name)

    # 1. 统一所有标点符号、特殊符号为下划线 _（保留中文字符、英文字母、数字、下划线）
    new_base = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', base)

    # 2. 把当前目录名称（父目录）如果在文件名中出现过，则去掉
    parent_dir_name = os.path.basename(root)
    if parent_dir_name:
        # 对父目录名称本身也进行相同的符号清洗，以便准确匹配
        cleaned_parent = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', parent_dir_name)
        cleaned_parent = re.sub(r'_+', '_', cleaned_parent).strip('_')
        if cleaned_parent and cleaned_parent in new_base:
            new_base = new_base.replace(cleaned_parent, '')

    # 把连续的下划线合并为一个，并去除首尾多余的下划线
    new_base = re.sub(r'_+', '_', new_base)
    new_base = new_base.strip('_')

    new_name = new_base + ext

    # 计算相对路径，保证子目录结构被继承
    rel_path = os.path.relpath(root, source_base)
    if rel_path == '.':
        new_root = target_base
    else:
        new_root = os.path.join(target_base, rel_path)

    # 确保目标文件夹存在
    os.makedirs(new_root, exist_ok=True)

    new_path = os.path.join(new_root, new_name)

    try:
        shutil.copy2(old_path, new_path)
        return True, f"Copied: {name} -> {new_name}"
    except Exception as e:
        return False, f"Error copying {name}: {e}"


def sanitize_and_copy(source_path, target_path, max_workers=16):
    tasks = []

    # 遍历获取所有需要处理的文件
    for root, dirs, files in os.walk(source_path, topdown=False):
        for name in files:
            old_path = os.path.join(root, name)
            tasks.append((old_path, root, name, source_path, target_path))

    total_files = len(tasks)
    if total_files == 0:
        print("未找到任何文件。")
        return

    # 使用多线程池处理（文件拷贝 I/O 密集，释放 GIL 收益高）
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_single_file, *task): task
            for task in tasks
        }

        # 使用 tqdm 渲染整体进度条
        for future in tqdm(as_completed(futures), total=total_files, desc="复制并清理文件"):
            success, msg = future.result()
            if not success:
                tqdm.write(msg)


if __name__ == '__main__':
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 输入目录: ./data/media
    MEDIA_DIR = os.path.abspath(os.path.join(BASE_DIR, "data", "media"))
    # 输出目录: ./data/clean
    TARGET_DIR = os.path.abspath(os.path.join(BASE_DIR, "data", "clean"))

    if os.path.isdir(MEDIA_DIR):
        print(f"源目录: {MEDIA_DIR}")
        print(f"输出目录: {TARGET_DIR}")
        sanitize_and_copy(MEDIA_DIR, TARGET_DIR, max_workers=16)
        print("处理完成！")
    else:
        print("输入的源路径无效或不是目录。")