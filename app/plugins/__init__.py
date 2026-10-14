"""插件层：结构贡献与能力实现的来源。

插件分**必需**（loop / protocol / toolbox：可替换、不可停用）与**可选**（compaction /
approvals / webui / 工作区插件……）两种；必需性由装配脚本声明（`ctx.plugin(...,
essential=True)`），启动时校验"必需的都在 ACTIVE"。

目录：`builtin/` 是内核自带的一批；`discover.py`（阶段 E）负责工作区/全局的发现与信任判定。
"""
