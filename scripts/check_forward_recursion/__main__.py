import sys


def main():
    """这组用例复用 `check_mcp_chat_enrichment.py` 的隔离夹具，没有第二套。

    夹具（OneBot 桩、MCP 服务、会话）都在主门禁里，这里不复制一份——复制出来的
    夹具与真实接线无关，只会让「绿了但没接线」的假绿重新长回来。所以包目录只做
    拆分（`check_module_size.py` 的 1000 行硬上限），入口仍旧是主门禁。
    """
    print(__doc__)
    print('请运行：python scripts/check_mcp_chat_enrichment.py')
    return 2


if __name__ == '__main__':
    sys.exit(main())
