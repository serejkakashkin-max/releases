from __future__ import annotations

from typing import List, Optional

from services.employee_directory_service import (
    EmployeeDirectoryRuntimeContext,
    get_release_zni_users as _get_release_zni_users,
)


def get_release_zni_users(
    context: Optional[EmployeeDirectoryRuntimeContext] = None,
) -> List[str]:
    return list(_get_release_zni_users(context))
