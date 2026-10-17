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

# 目标特殊字符（用户要求直接删除包含 U+009F 的整行）
TARGET_CHAR = '\x9f'


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
    检查代理的所有字符串字段。
    """
    bad_names: Set[str] = set()
    for proxy in proxies:
        name = proxy.get('name')
        if not name:
            continue
        # 检查代理对象中所有字符串值
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
    1. 从 proxies 列表中移除坏的代理名
    2. 如果代理组的 proxies 变空，移除整个代理组
    """
    cleaned_groups = []
    for group in proxy_groups:
        proxies = group.get('proxies', [])
        if not isinstance(proxies, list):
            cleaned_groups.append(group)
            continue

        filtered = [p for p in proxies if p not in bad_names]
        if filtered:
            new_group = group.copy()
            new_group['proxies'] = filtered
            cleaned_groups.append(new_group)
        # 如果 filtered 为空，丢弃该代理组
    return cleaned_groups


def clean_clash_config(input_path: Path, output_path: Optional[Path] = None, inplace: bool = False) -> Dict[str, Any]:
    """
    清理 Clash 配置文件。
    返回清理后的配置字典。

    改动：为了避免 YAML 解析器因不可接受字符 U+009F 报错，
    1) 先删除所有包含 U+009F（\x9f）的整行；
    2) 对剩余内容仍然保留之前的不可接受字符替换策略以便解析。
    """
    with input_path.open('r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # 直接删除包含 U+009F 的行（按用户要求）
    if chr(0x9F) in content:
        lines = content.splitlines(keepends=True)
        new_lines = [l for l in lines if chr(0x9F) not in l]
        removed = len(lines) - len(new_lines)
        if removed > 0:
            print(f"已删除 {removed} 行包含 U+009F 的不安全字符。")
        content = ''.join(new_lines)

    config = None

    # 如果内容中还有 YAML 不可接受的控制字符，先进行替换以避免解析直接失败
    if YAML_UNACCEPTABLE_PATTERN.search(content):
        escaped = _escape_control_chars_for_parsing(content)
        try:
            config = yaml.safe_load(escaped)
            print("已替换不可接受控制字符为占位符以继续解析。")
        except yaml.YAMLError as e:
            print(f"尝试解析替换后的内容仍然失败: {e}", file=sys.stderr)
            # 退回到更宽松的加载器
            try:
                config = yaml.load(escaped, Loader=yaml.FullLoader)
            except Exception as e2:
                print(f"解析仍然失败: {e2}", file=sys.stderr)
                return {}
    else:
        # 不存在不可接受控制字符，直接尝试安全加载
        try:
            config = yaml.safe_load(content)
        except yaml.YAMLError as e:
            # 如果解析失败（但不是因为不可接受字符），打印并尝试更宽松的 FullLoader
            print(f"YAML 解析错误: {e}", file=sys.stderr)
            try:
                config = yaml.load(content, Loader=yaml.FullLoader)
            except Exception as e2:
                print(f"解析仍然失败: {e2}", file=sys.stderr)
                return {}

    if not config:
        print("配置为空", file=sys.stderr)
        return {}

    proxies = config.get('proxies', [])
    proxy_groups = config.get('proxy-groups', config.get('proxies_group', []))

    if not proxies:
        print("未找到 proxies 段", file=sys.stderr)
        return config

    # 找出有控制字符的代理
    bad_names = find_proxies_with_control_chars(proxies)
    print(f"发现 {len(bad_names)} 个包含控制字符的代理: {sorted(bad_names)}")

    # 过滤 proxies
    clean_proxies = [p for p in proxies if p.get('name') not in bad_names]
    print(f"proxies: {len(proxies)} -> {len(clean_proxies)}")

    # 过滤 proxy-groups
    clean_groups = filter_proxy_groups(proxy_groups, bad_names)
    print(f"proxy-groups: {len(proxy_groups)} -> {len(clean_groups)}")

    # 构建新配置
    new_config = config.copy()
    new_config['proxies'] = clean_proxies
    # 保持原 key 名称
    if 'proxy-groups' in config:
        new_config['proxy-groups'] = clean_groups
    elif 'proxies_group' in config:
        new_config['proxies_group'] = clean_groups

    # 写入输出
    out_path = output_path or (input_path if inplace else input_path.with_suffix('.clean.yaml'))
    if inplace:
        backup_path = input_path.with_suffix(input_path.suffix + '.bak')
        input_path.rename(backup_path)
        print(f"已备份原文件到: {backup_path}")

    with out_path.open('w', encoding='utf-8') as f:
        yaml.dump(new_config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    print(f"已写入: {out_path}")
    return new_config

def main():
    parser = argparse.ArgumentParser(description='清理 Clash 配置中的控制字符代理')
    parser.add_argument('input', help='输入 YAML 文件路径')
    parser.add_argument('output', nargs='?', help='输出 YAML 文件路径（可选，默认就地修改并备份）')
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
