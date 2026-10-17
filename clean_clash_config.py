#!/usr/bin/env python3
"""
Clash 配置清理工具：移除包含控制字符的代理及其在代理组中的引用。

用法:
    python clean_clash_config.py input.yaml output.yaml
    python clean_clash_config.py input.yaml  # 就地修改，备份为 input.yaml.bak
"""

import sys
import re
import yaml
import argparse
from pathlib import Path
from typing import Any, Dict, List, Set, Optional

# 1) 普通控制字符检测：包含 C0 和 C1 区间
CONTROL_CHAR_PATTERN = re.compile(r'[\x00-\x1f\x7f-\x9f]')

# 2) YAML 不可接受字符（safe_load 会直接报错）
#    这部分不包含 \t / \n / \r，因为它们是合法的 YAML。
YAML_UNACCEPTABLE_PATTERN = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')

# 3) 占位符前缀：替换不可接受字符后，用它识别原来是否存在控制符
CTRL_MARKER_PREFIX = "__CTRL_"


def has_control_chars(text: str) -> bool:
    """检查字符串是否包含控制字符，或此前被替换成标记的文本。"""
    if not isinstance(text, str):
        return False
    if CONTROL_CHAR_PATTERN.search(text):
        return True
    if CTRL_MARKER_PREFIX in text:
        return True
    return False


def _escape_control_chars_for_parsing(s: str) -> str:
    """把 YAML 不可接受字符替换成可见占位符，便于解析并保留原位置信息。"""
    def repl(m):
        ch = m.group(0)
        return f"{CTRL_MARKER_PREFIX}{ord(ch):02x}__"
    return YAML_UNACCEPTABLE_PATTERN.sub(repl, s)


def find_proxies_with_control_chars(proxies: List[Dict[str, Any]]) -> Set[str]:
    """
    找出包含控制字符的代理名。
    检查代理对象中所有字符串字段，递归遍历 dict/list。
    """
    bad_names: Set[str] = set()

    for proxy in proxies:
        if not isinstance(proxy, dict):
            continue

        name = proxy.get('name')
        if not name:
            continue

        def check_value(val: Any) -> bool:
            if isinstance(val, str):
                return has_control_chars(val)
            if isinstance(val, dict):
                return any(check_value(v) for v in val.values())
            if isinstance(val, list):
                return any(check_value(v) for v in val)
            return False

        if check_value(proxy):
            bad_names.add(name)

    return bad_names


def filter_proxy_groups(proxy_groups: List[Dict[str, Any]], bad_names: Set[str]) -> List[Dict[str, Any]]:
    """
    过滤代理组：
    1) 从 group['proxies'] 中剔除坏代理名
    2) 如果代理组变空，则整体删除
    """
    cleaned_groups = []

    if not isinstance(proxy_groups, list):
        return cleaned_groups

    for group in proxy_groups:
        if not isinstance(group, dict):
            continue

        proxies = group.get('proxies', [])
        if not isinstance(proxies, list):
            cleaned_groups.append(group)
            continue

        filtered = [p for p in proxies if p not in bad_names]
        if filtered:
            new_group = group.copy()
            new_group['proxies'] = filtered
            cleaned_groups.append(new_group)

    return cleaned_groups


def _load_yaml_content(content: str) -> Optional[Dict[str, Any]]:
    """
    含鲁棒性地加载 YAML，并返回第一个 dict/mapping。
    处理：
    - safe_load 直接返回 str / list / None
    - 多文档 YAML
    - YAML 文本内容中嵌套了字符串
    """
    def try_parse(text: str) -> Optional[Any]:
        try:
            return yaml.safe_load(text)
        except Exception:
            return None

    # 1) 先安全解析一次
    parsed = try_parse(content)

    # 直接是 dict
    if isinstance(parsed, dict):
        return parsed

    # 若是字符串，说明可能是“字符串内容本身就是 YAML”
    if isinstance(parsed, str):
        inner = try_parse(parsed)
        if isinstance(inner, dict):
            return inner

    # 若是 list，找第一个 dict；若其中还有字符串，再解析
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                return item
        for item in parsed:
            if isinstance(item, str):
                inner = try_parse(item)
                if isinstance(inner, dict):
                    return inner

    # 2) 多文档 YAML：返回第一个 dict
    try:
        for doc in yaml.load_all(content, Loader=yaml.FullLoader):
            if isinstance(doc, dict):
                return doc
            if isinstance(doc, str):
                inner = try_parse(doc)
                if isinstance(inner, dict):
                    return inner
    except Exception:
        pass

    # 3) 最后，FullLoader 兜底
    try:
        parsed2 = yaml.load(content, Loader=yaml.FullLoader)
        if isinstance(parsed2, dict):
            return parsed2
        if isinstance(parsed2, list):
            for item in parsed2:
                if isinstance(item, dict):
                    return item
                if isinstance(item, str):
                    inner = try_parse(item)
                    if isinstance(inner, dict):
                        return inner
    except Exception:
        pass

    # 诊断输出：帮助定位为什么不是 dict
    parsed_type = type(parsed).__name__ if parsed is not None else "None"
    snippet = (str(parsed)[:200] + '...') if parsed is not None else ""
    print(f"无法从 YAML 中提取到 mapping（dict）。得到类型: {parsed_type}。样例: {snippet}", file=sys.stderr)
    return None


def clean_clash_config(input_path: Path, output_path: Optional[Path] = None, inplace: bool = False) -> Dict[str, Any]:
    """
    清理 Clash 配置文件：
    1) 将 U+009F 从内容中移除（但不删除整行）
    2) 解析 YAML
    3) 找出带控制字符的代理并移除
    4) 清理 proxy-groups 中的引用
    5) 写出新 YAML
    """
    with input_path.open('r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # 不要删除整行 —— 直接移除 U+009F 字符以保留 YAML 结构
    bad_char = chr(0x9F)
    count_bad = content.count(bad_char)
    if count_bad:
        content = content.replace(bad_char, '')
        print(f"已移除 {count_bad} 个 U+009F 控制字符（仅移除字符，不删除行）。")

    # 若还有其他 YAML 不可接受字符，再做占位符替换后解析
    config: Optional[Dict[str, Any]] = None
    used_text_for_parsing = content
    if YAML_UNACCEPTABLE_PATTERN.search(content):
        escaped = _escape_control_chars_for_parsing(content)
        used_text_for_parsing = escaped
        config = _load_yaml_content(escaped)
        if config is not None:
            print("已替换不可接受的控制字符为占位符，并继续解析。")
        else:
            print("解析替换后的内容仍无法得到 mapping（dict）", file=sys.stderr)
            try:
                maybe = yaml.safe_load(escaped)
                print(f"诊断: safe_load(escaped) 返回类型 {type(maybe).__name__}", file=sys.stderr)
            except Exception as e:
                print(f"诊断: safe_load(escaped) 抛出: {e}", file=sys.stderr)
            return {}
    else:
        config = _load_yaml_content(content)
        if config is None:
            print("YAML 解析未能返回 mapping（dict），请检查输入文件格式。", file=sys.stderr)
            try:
                maybe = yaml.safe_load(content)
                print(f"诊断: safe_load(content) 返回类型 {type(maybe).__name__}", file=sys.stderr)
            except Exception as e:
                print(f"诊断: safe_load(content) 抛出: {e}", file=sys.stderr)
            return {}

    # 最终保障：确保 config 是 dict
    if not isinstance(config, dict):
        # 尝试再次用 used_text_for_parsing 解析多文档
        try:
            for doc in yaml.load_all(used_text_for_parsing, Loader=yaml.FullLoader):
                if isinstance(doc, dict):
                    config = doc
                    break
        except Exception:
            pass

    if not isinstance(config, dict):
        print("最终无法得到 mapping（dict），终止处理。", file=sys.stderr)
        return {}

    if not config:
        print("配置为空", file=sys.stderr)
        return {}

    proxies = config.get('proxies', [])
    proxy_groups = config.get('proxy-groups', config.get('proxies_group', []))

    # 如果没有 proxies 字段，直接返回原配置
    if not proxies:
        print("未找到 proxies 段", file=sys.stderr)
        return config

    # 找出坏代理名
    bad_names = find_proxies_with_control_chars(proxies)
    print(f"发现 {len(bad_names)} 个包含控制字符的代理: {sorted(bad_names)}")

    # 清理 proxies
    clean_proxies = [p for p in proxies if isinstance(p, dict) and p.get('name') not in bad_names]
    print(f"proxies: {len(proxies)} -> {len(clean_proxies)}")

    # 清理 proxy-groups
    clean_groups = filter_proxy_groups(proxy_groups, bad_names)
    print(f"proxy-groups: {len(proxy_groups)} -> {len(clean_groups)}")

    # 重建配置
    new_config = config.copy()
    new_config['proxies'] = clean_proxies

    if 'proxy-groups' in config:
        new_config['proxy-groups'] = clean_groups
    elif 'proxies_group' in config:
        new_config['proxies_group'] = clean_groups

    # 输出目标文件
    if inplace:
        backup_path = input_path.with_suffix(input_path.suffix + '.bak')
        # 先备份原文件
        if input_path.exists():
            input_path.rename(backup_path)
            print(f"已备份原文件到: {backup_path}")
        out_path = input_path
    else:
        out_path = output_path or input_path.with_suffix('.clean.yaml')

    with out_path.open('w', encoding='utf-8') as f:
        yaml.dump(new_config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    print(f"已写入: {out_path}")
    return new_config


def main():
    parser = argparse.ArgumentParser(description='清理 Clash 配置中的控制字符代理')
    parser.add_argument('input', help='输入 YAML 文件路径')
    parser.add_argument('output', nargs='?', help='输出 YAML 文件路径（可选，默认输出为 .clean.yaml；若 --inplace，则直接覆盖源文件并备份）')
    parser.add_argument('--inplace', '-i', action='store_true', help='就地修改输入文件（会创建 .bak 备份）')
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else None
    clean_clash_config(input_path, output_path, args.inplace)


if __name__ == '__main__':
    main()
