"""Implementation policy shared by desktop and legacy execution."""

MINIMAL_IMPLEMENTATION_RULES = """实现决策原则（edit 模式必须遵守，按顺序优先）：
1. 这个功能真的需要存在吗？不需要就跳过（YAGNI）。
2. 代码库里已有可复用的实现？直接用，不重复造轮子。
3. 标准库或语言内置能覆盖？用标准库，不引入外部依赖。
4. 平台原生特性能满足？用原生（例如 <input type="date"> 而非引入日期选择库）。
5. 已安装的依赖能处理？用现有依赖，不新增包。
6. 能用一行解决？就写一行。
7. 以上都不满足：只写恰好够用的最小实现，不加"以后可能用到"的抽象。
"""
