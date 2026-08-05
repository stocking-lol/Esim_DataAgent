"""
密码加密工具
------------
使用 bcrypt 提供密码哈希与验证。
"""

import bcrypt


def hash_password(password: str) -> str:
    """对明文密码进行 bcrypt 哈希

    Args:
        password: 明文密码

    Returns:
        str: bcrypt 哈希字符串
    """
    # bcrypt 限制密码最大 72 字节，超出部分截断
    pwd_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """验证明文密码与哈希是否匹配

    Args:
        password: 明文密码
        hashed: bcrypt 哈希字符串

    Returns:
        bool: 匹配返回 True，否则 False
    """
    pwd_bytes = password.encode("utf-8")[:72]
    hashed_bytes = hashed.encode("utf-8")
    return bcrypt.checkpw(pwd_bytes, hashed_bytes)
