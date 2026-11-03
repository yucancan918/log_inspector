import re

FIX_PATTERNS = [
    # f"?..." 开头但结尾没引号的
    (re.compile(r'(\s*)f"\?(.+?)\)?\s*$'), lambda m: f'{m.group(1)}f"✓{m.group(2)}?'),
]

with open("log_inspector.py", "r", encoding="utf-8") as f:
    content = f.read()

lines = content.split("\n")
fix_count = 0

while True:
    try:
        compile("\n".join(lines), "log_inspector.py", "exec")
        print("全部语法OK!")
        break
    except SyntaxError as e:
        ln_idx = e.lineno - 1
        bad_line = lines[ln_idx]
        print(f"修 Line {e.lineno}: {bad_line.strip()[:120]}")

        # 策略1: 找 f"...?" 结尾的，补个 "
        m = re.match(r'^(\s*f["\'])(.+)(\?)\)?\s*$', bad_line)
        if m and '"' in bad_line and bad_line.count('"') % 2 != 0:
            # 双引号奇数，补一个
            lines[ln_idx] = bad_line.rstrip() + '"'
            print(f"  -> 补了结尾双引号")
            fix_count += 1
            continue

        # 策略2: 如果是字符串字面量 "..." 没闭合
        m = re.match(r'^(\s*)"([^"\n]*)$', bad_line)
        if m and bad_line.count('"') % 2 != 0:
            lines[ln_idx] = bad_line.rstrip() + '"'
            print(f"  -> 补了字符串结尾双引号")
            fix_count += 1
            continue

        # 策略3: 单引号同理
        m = re.match(r"^(\s*)'([^'\n]*)$", bad_line)
        if m and bad_line.count("'") % 2 != 0 and '"' not in bad_line:
            lines[ln_idx] = bad_line.rstrip() + "'"
            print(f"  -> 补了字符串结尾单引号")
            fix_count += 1
            continue

        print(f"  !!! 无法自动修复，跳过")
        break

with open("log_inspector.py", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"\n总共自动修复 {fix_count} 处")