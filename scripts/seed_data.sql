-- ============================================================
-- AI_Assistant 种子数据脚本
-- 在 init_all_tables.sql 执行之后运行
-- 创建默认租户和 4 个不同角色的测试用户
-- ============================================================

-- 1. 创建默认租户
INSERT INTO t_tenant (name, code)
    SELECT '系统默认租户', 'default'
    WHERE NOT EXISTS (SELECT 1 FROM t_tenant WHERE code = 'default');

-- 2. 创建各角色用户（密码见注释，均使用 bcrypt 加密，cost=4）
--    正式环境请使用更高的 cost 值（如 12）
--
-- 用户清单：
--   admin        / admin123    → super_admin    系统超级管理员
--   tenant_admin / tenant123   → tenant_admin    租户管理员
--   editor       / editor123   → editor          编辑者
--   viewer       / viewer123   → viewer          查看者

-- super_admin: 系统超级管理员
INSERT INTO t_user (username, password_hash, display_name, role, tenant_id)
    SELECT 'admin', '$2b$04$bXcvKt/QCF6IJbELJvv8oONJL2hw06gPaOqUn/8LcEdRIE1wHGU4i', '系统管理员', 'super_admin', id
    FROM t_tenant WHERE code = 'default'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'admin');

-- tenant_admin: 租户管理员
INSERT INTO t_user (username, password_hash, display_name, role, tenant_id)
    SELECT 'tenant_admin', '$2b$04$ZacNDisHUtCFgspSivwuEO1vlK5xT678pom4VAr/ihgx9ucHFSXg.', '租户管理员', 'tenant_admin', id
    FROM t_tenant WHERE code = 'default'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'tenant_admin');

-- editor: 编辑者
INSERT INTO t_user (username, password_hash, display_name, role, tenant_id)
    SELECT 'editor', '$2b$04$1lZL..nC5Rr4pKHo.EZiQeYL1vEo1GNZSf79zCMeuF0Z.qyArVcjC', '编辑者', 'editor', id
    FROM t_tenant WHERE code = 'default'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'editor');

-- viewer: 查看者
INSERT INTO t_user (username, password_hash, display_name, role, tenant_id)
    SELECT 'viewer', '$2b$04$A7Dyq3QC2ymp9XGwuaxMs.nrqzcVpnVDi0YSmVsPugtItNph7vxAm', '查看者', 'viewer', id
    FROM t_tenant WHERE code = 'default'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'viewer');
