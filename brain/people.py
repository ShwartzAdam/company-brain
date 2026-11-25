from brain import db


def is_manager(employee_id: str, path=None) -> bool:
    """Rule: a manager is someone who is another employee's manager_id (not user_group)."""
    return bool(db.rows("SELECT 1 FROM employees WHERE manager_id = ? LIMIT 1", (employee_id,), path))
