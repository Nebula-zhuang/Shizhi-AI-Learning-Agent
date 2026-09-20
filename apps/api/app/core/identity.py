"""身份相关的常量。

放在 `app/core/` 是为了让**模型层**和**业务层**能引用同一个定义而不产生循环依赖 ——
`app/models/*` 不应该 import `app/services/*`。

## `DEFAULT_LEARNER_ID` 到底是什么

它是**未登录时**的学习档案标识，也是 P0–P5 时期唯一的那个档案（那时还没有账号）。

引入账号之后它有了第二个用途：**演示账号的 learner_id 就是它** ——
这样 P0–P5 攒下的样例数据（资料、知识点、学习进度）能被演示账号看到。

同一条规则也用在**资料归属**上：匿名上传的资料归到 `local`，
所以本机调试、接口文档试用、自动化测试都不会因为"没有身份"而存不进去。
"""

from __future__ import annotations

#: 未登录（或本机匿名）时使用的学习档案标识。
DEFAULT_LEARNER_ID = "local"

__all__ = ["DEFAULT_LEARNER_ID"]
