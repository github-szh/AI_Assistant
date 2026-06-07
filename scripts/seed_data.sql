-- ============================================================
-- AI_Assistant 种子数据脚本
-- 在 init_all_tables.sql 执行之后运行
-- 创建默认租户和 4 个不同角色的测试用户
-- ============================================================

-- 1. 创建默认租户
INSERT INTO t_tenant (name, code)
    SELECT '系统默认租户', 'default'
    WHERE NOT EXISTS (SELECT 1 FROM t_tenant WHERE code = 'default');

-- 2. 插入权限定义
INSERT INTO t_permission (code, name) VALUES
    ('document:view', 'View Documents'),
    ('document:upload', 'Upload Documents'),
    ('document:delete', 'Delete Documents'),
    ('chat:send', 'Send Messages'),
    ('chat:view', 'View Sessions'),
    ('chat:delete', 'Delete Sessions'),
    ('knowledge:query', 'Query Knowledge Base'),
    ('quality:view', 'View Quality Evaluation'),
    ('quality:admin', 'Admin Quality Evaluation'),
    ('tenant:manage', 'Manage Tenant'),
    ('tenant:users:manage', 'Manage Users'),
    ('system:settings:view', 'View System Settings'),
    ('*', 'All Permissions');

-- 3. 插入角色定义
INSERT INTO t_role (name, description) VALUES
    ('super_admin', '系统超级管理员'),
    ('tenant_admin', '租户管理员'),
    ('editor', '编辑者'),
    ('viewer', '查看者');

-- 4. 角色-权限关联
-- super_admin → *
INSERT INTO t_role_permission (role_id, permission_id)
    SELECT r.id, p.id FROM t_role r, t_permission p WHERE r.name = 'super_admin' AND p.code = '*';

-- tenant_admin → 除 * 外的所有权限
INSERT INTO t_role_permission (role_id, permission_id)
    SELECT r.id, p.id FROM t_role r, t_permission p
    WHERE r.name = 'tenant_admin' AND p.code IN (
        'tenant:manage', 'tenant:users:manage',
        'document:upload', 'document:view', 'document:delete',
        'chat:send', 'chat:view', 'chat:delete',
        'knowledge:query',
        'quality:view', 'quality:admin',
        'system:settings:view'
    );

-- editor
INSERT INTO t_role_permission (role_id, permission_id)
    SELECT r.id, p.id FROM t_role r, t_permission p
    WHERE r.name = 'editor' AND p.code IN (
        'document:upload', 'document:view', 'document:delete',
        'chat:send', 'chat:view', 'chat:delete',
        'knowledge:query'
    );

-- viewer
INSERT INTO t_role_permission (role_id, permission_id)
    SELECT r.id, p.id FROM t_role r, t_permission p
    WHERE r.name = 'viewer' AND p.code IN (
        'document:view',
        'chat:send', 'chat:view', 'chat:delete',
        'knowledge:query'
    );

-- 5. 创建各角色用户（密码见注释，均使用 bcrypt 加密，cost=4）
--    正式环境请使用更高的 cost 值（如 12）
--
-- 用户清单：
--   admin        / admin123    → super_admin    系统超级管理员
--   tenant_admin / tenant123   → tenant_admin    租户管理员
--   editor       / editor123   → editor          编辑者
--   viewer       / viewer123   → viewer          查看者

-- super_admin: 系统超级管理员
INSERT INTO t_user (username, password_hash, display_name, role_id, tenant_id)
    SELECT 'admin', '$2b$04$bXcvKt/QCF6IJbELJvv8oONJL2hw06gPaOqUn/8LcEdRIE1wHGU4i', '系统管理员', r.id, t.id
    FROM t_tenant t, t_role r WHERE t.code = 'default' AND r.name = 'super_admin'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'admin');

-- tenant_admin: 租户管理员
INSERT INTO t_user (username, password_hash, display_name, role_id, tenant_id)
    SELECT 'tenant_admin', '$2b$04$ZacNDisHUtCFgspSivwuEO1vlK5xT678pom4VAr/ihgx9ucHFSXg.', '租户管理员', r.id, t.id
    FROM t_tenant t, t_role r WHERE t.code = 'default' AND r.name = 'tenant_admin'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'tenant_admin');

-- editor: 编辑者
INSERT INTO t_user (username, password_hash, display_name, role_id, tenant_id)
    SELECT 'editor', '$2b$04$1lZL..nC5Rr4pKHo.EZiQeYL1vEo1GNZSf79zCMeuF0Z.qyArVcjC', '编辑者', r.id, t.id
    FROM t_tenant t, t_role r WHERE t.code = 'default' AND r.name = 'editor'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'editor');

-- viewer: 查看者
INSERT INTO t_user (username, password_hash, display_name, role_id, tenant_id)
    SELECT 'viewer', '$2b$04$A7Dyq3QC2ymp9XGwuaxMs.nrqzcVpnVDi0YSmVsPugtItNph7vxAm', '查看者', r.id, t.id
    FROM t_tenant t, t_role r WHERE t.code = 'default' AND r.name = 'viewer'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'viewer');

-- ============================================================
-- 创建第二个租户及其用户
-- ============================================================

-- 6. 创建第二个租户（客户A租户）
INSERT INTO t_tenant (name, code)
    SELECT '客户A租户', 'client_a'
    WHERE NOT EXISTS (SELECT 1 FROM t_tenant WHERE code = 'client_a');

-- 7. 创建第二个租户的用户（不同角色）
-- 用户清单：
--   client_admin  / client123   → tenant_admin  客户A管理员
--   client_editor / client123   → editor         客户A编辑者
--   client_viewer / client123   → viewer         客户A查看者

-- client_admin: 客户A管理员（tenant_admin 角色）
INSERT INTO t_user (username, password_hash, display_name, role_id, tenant_id)
    SELECT 'client_admin', '$2b$04$ZacNDisHUtCFgspSivwuEO1vlK5xT678pom4VAr/ihgx9ucHFSXg.', '客户A管理员', r.id, t.id
    FROM t_tenant t, t_role r WHERE t.code = 'client_a' AND r.name = 'tenant_admin'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'client_admin');

-- client_editor: 客户A编辑者（editor 角色）
INSERT INTO t_user (username, password_hash, display_name, role_id, tenant_id)
    SELECT 'client_editor', '$2b$04$1lZL..nC5Rr4pKHo.EZiQeYL1vEo1GNZSf79zCMeuF0Z.qyArVcjC', '客户A编辑者', r.id, t.id
    FROM t_tenant t, t_role r WHERE t.code = 'client_a' AND r.name = 'editor'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'client_editor');

-- client_viewer: 客户A查看者（viewer 角色）
INSERT INTO t_user (username, password_hash, display_name, role_id, tenant_id)
    SELECT 'client_viewer', '$2b$04$A7Dyq3QC2ymp9XGwuaxMs.nrqzcVpnVDi0YSmVsPugtItNph7vxAm', '客户A查看者', r.id, t.id
    FROM t_tenant t, t_role r WHERE t.code = 'client_a' AND r.name = 'viewer'
    AND NOT EXISTS (SELECT 1 FROM t_user WHERE username = 'client_viewer');
