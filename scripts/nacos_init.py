"""Nacos 五 dataId 初值写入脚本（占位——批次 1 实装）。

职责（docs/ARCHITECTURE.md §3.1 scripts/nacos_init.py 锚点）：
    向 Nacos 写入五个 dataId 的初值（五 dataId 的键位与默认值见
    docs/contract/nacos-config.md；本脚本不写入任何密钥——密钥绝不入 Nacos）。

执行口径（docs/contract/nacos-config.md §8 双账号）：
    - 本脚本用【管理员账号】执行——管理员仅初始化与维护用；
    - 运行态客户端一律用专用只读账号 ai-search-ro（读+监听、无写权限），
      由初始化流程创建并授权（ARCHITECTURE §7 NACOS_ 组注）；
    - 前置：deploy compose 起 nacos（standalone + 鉴权开启，NACOS_AUTH_* 三件套
      经 .env 引用注入容器，ARCHITECTURE §5/§7），管理员改密后方可执行。

现状：批次 0 仅落目录锚点；上述写入逻辑在批次 1 实装。
"""

import sys


def main() -> None:
    print("scripts/nacos_init.py 目前是占位脚本：")
    print("  - 五 dataId 初值写入逻辑在批次 1 实装（键位见 docs/contract/nacos-config.md）；")
    print("  - 执行走 nacos-config.md §8 双账号口径：管理员账号初始化，")
    print("    专用只读账号 ai-search-ro 供运行态客户端（读+监听、无写权限）；")
    print("  - 前置：deploy compose 起 nacos（standalone + 鉴权开启，NACOS_AUTH_* 注入容器）")
    print("    且管理员改密完成。")
    raise NotImplementedError("nacos_init.py 尚未实装（批次 1 工单交付）")


if __name__ == "__main__":
    sys.exit(main())
