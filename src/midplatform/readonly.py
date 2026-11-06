"""检查数据源凭据；只读事务只是额外的一层保护。"""


class ReadOnlyRoleRequired(ValueError):
    """安全的配置指引，不含原始 DSN 或驱动错误文本。"""


def ensure_postgres_reader(connection):
    """确认当前 PostgreSQL 角色是专用只读账号：不能有超级用户或管理权限，也不能有任何表写入、建表或建库权限，否则抛出 ReadOnlyRoleRequired。"""
    role = connection.execute(
        "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
        "FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    if not role or role[0]:
        raise ReadOnlyRoleRequired("业务数据源必须使用专用只读账号，不能使用超级用户或管理角色")
    writes = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' "
        "AND c.relkind IN ('r','p','v','m','f') "
        "AND has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')) "
        "OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg_%' "
        "AND n.nspname<>'information_schema' AND has_schema_privilege(current_user,n.oid,'CREATE')) "
        "OR has_database_privilege(current_user,current_database(),'CREATE')"
    ).fetchone()
    if not writes or writes[0]:
        raise ReadOnlyRoleRequired("业务账号仍有写入或建表权限，请由数据库管理员提供专用只读账号")
