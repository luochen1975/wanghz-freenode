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


# 检测常规控制字符（包含 C0 和 C1 区间，便于在内存中判断）
CONTROL_CHAR_PATTERN = re.compile(r'[\x00-\x1f\x7f-\x9f]')

# YAML 解析器对某些控制字符（不可接受字符）会报错，需要在解析前替换它们。
# 注意：不替换常见的制表/换行符（\t=\x09, \n=\x0a, \r=\x0d）。
YAML_UNACCEPTABLE_PATTERN = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')
# 占位符前缀，用于替换后识别
CTRL_MARKER_PREFIX = "__CTRL_"


def has_control_chars(text: str) -> bool:
    """检查字符串是否包含控制字符或此前替换的占位符。"""
    if not isinstance(text, str):
        return False
    if CONTROL_CHAR_PATTERN.search(text):
        return True
    if CTRL_MARKER_PREFIX in text:
        return True
    return False


def _escape_control_chars_for_parsing(s: str) -> str:
    """把 YAML 不可接受的控制字符替换成可见的占位符，方便 safe_load 成功解析并能检测到原来有控制字符的位置。"""
    def repl(m):
        ch = m.group(0)
        return f"{CTRL_MARKER_PREFIX}{ord(ch):02x}__"
    return YAML_UNACCEPTABLE_PATTERN.sub(repl, s)


def find_proxies_with_control_chars(proxies: List[Dict[str, Any]]) -> Set[str]:
    """
    找出包含控制字符的代理名称。
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
    读取 YAML 并返回第一个 dict/mapping。
    处理 safe_load 直接返回 str/list/None、多文档 YAML 和字符串包 YAML 等情况。
    """
    def try_parse(text: str) -> Optional[Any]:
        try:
            return yaml.safe_load(text)
        except Exception:
            return None

    parsed = try_parse(content)

    if isinstance(parsed, dict):
        return parsed

    if isinstance(parsed, str):
        inner = try_parse(parsed)
        if isinstance(inner, dict):
            return inner

    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                return item
        for item in parsed:
            if isinstance(item, str):
                inner = try_parse(item)
                if isinstance(inner, dict):
                    return inner

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

    parsed_type = type(parsed).__name__ if parsed is not None else "None"
    snippet = (str(parsed)[:200] + '...') if parsed is not None else ""
    print(f"无法从 YAML 中提取到 mapping（dict）。得到类型: {parsed_type}。样例: {snippet}", file=sys.stderr)
    return None


def clean_clash_config(input_path: Path, output_path: Optional[Path] = None, inplace: bool = False) -> Dict[str, Any]:
    """
    清理 Clash 配置文件：
    1) 移除 U+009F 字符（不删除整行）
    2) 解析 YAML
    3) 找出带控制字符的代理并移除
    4) 清理 proxy-groups 中的引用
    5) 写出新 YAML
    """
    with input_path.open('r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # 不要删除整行，直接移除 U+009F 字符，保留 YAML 结构
    bad_char = chr(0x9F)
    count_bad = content.count(bad_char)
    if count_bad:
        content = content.replace(bad_char, '')
        print(f"已移除 {count_bad} 个 U+009F 控制字符（仅移除字符，不删除行）。")

    used_text_for_parsing = content
    config: Optional[Dict[str, Any]] = None

    # 若还有其他 YAML 不可接受字符，则先替换占位符再解析
    if YAML_UNACCEPTABLE_PATTERN.search(content):
        escaped = _escape_control_chars_for_parsing(content)
        used_text_for_parsing = escaped
        config = _load_yaml_content(escaped)
        if config is not None:
            print("已替换不可接受的控制字符为占位符，并继续解析。")
        else:
            print("解析替换后的内容仍无法得到 mapping（dict）", file=sys.stderr)
            return {}
    else:
        config = _load_yaml_content(content)
        if config is None:
            print("YAML 解析未能返回 mapping（dict），请检查输入文件格式。", file=sys.stderr)
            return {}

    if not isinstance(config, dict):
        if isinstance(config, str):
            try:
                maybe = yaml.safe_load(config)
                if isinstance(maybe, dict):
                    config = maybe
            except Exception:
                pass

        if not isinstance(config, dict):
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

    if not proxies:
        print("未找到 proxies 段", file=sys.stderr)
        return config

    bad_names = find_proxies_with_control_chars(proxies)
    print(f"发现 {len(bad_names)} 个包含控制字符的代理: {sorted(bad_names)}")

    clean_proxies = [p for p in proxies if isinstance(p, dict) and p.get('name') not in bad_names]
    print(f"proxies: {len(proxies)} -> {len(clean_proxies)}")

    clean_groups = filter_proxy_groups(proxy_groups, bad_names)
    print(f"proxy-groups: {len(proxy_groups)} -> {len(clean_groups)}")

    new_config = config.copy()
    new_config['proxies'] = clean_proxies

    if 'proxy-groups' in config:
        new_config['proxy-groups'] = clean_groups
    elif 'proxies_group' in config:
        new_config['proxies_group'] = clean_groups

    if inplace:
        backup_path = input_path.with_suffix(input_path.suffix + '.bak')
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
