"""
列级脱敏测试
------------
测试基于角色的数据脱敏功能。

测试覆盖：
  1. admin 角色不脱敏
  2. analyst 角色手机号脱敏
  3. viewer 角色 ICCID 脱敏（只显示后4位）
  4. analyst 角色 IMSI 全脱敏
  5. analyst 角色邮箱脱敏
  6. 多列同时脱敏
  7. 内容检测脱敏（字符串中的手机号）
  8. 脱敏模式验证
"""

import pytest

from app.services.masking_service import MaskingService, masking_service


class TestMaskingPatterns:
    """脱敏模式测试"""

    def test_mask_middle(self):
        """mask_middle: 138****5678"""
        result = masking_service._apply_pattern("13812345678", "mask_middle")
        assert result == "138****5678"

    def test_mask_all_except_last4(self):
        """mask_all_except_last4: ************1234"""
        result = masking_service._apply_pattern("8986012345678901234", "mask_all_except_last4")
        assert result.endswith("1234")
        assert result.count("*") == len("8986012345678901234") - 4

    def test_mask_all(self):
        """mask_all: ****"""
        result = masking_service._apply_pattern("460001234567890", "mask_all")
        assert result == "*" * 15

    def test_mask_email(self):
        """mask_email: z***@example.com"""
        result = masking_service._apply_pattern("zhangsan@example.com", "mask_email")
        assert result == "z***@example.com"

    def test_mask_generic(self):
        """mask_generic: 保留首尾"""
        result = masking_service.mask_generic("abcdef")
        assert result == "a****f"


class TestRoleBasedMasking:
    """基于角色的脱敏测试"""

    def test_admin_no_masking(self):
        """admin 角色不脱敏"""
        data = [
            {"id": 1, "phone_number": "13812345678", "email": "test@example.com"},
        ]
        masked_data, masked_cols = masking_service.mask_query_result(
            data, ["id", "phone_number", "email"], role="admin"
        )
        assert len(masked_cols) == 0
        assert masked_data[0]["phone_number"] == "13812345678"
        assert masked_data[0]["email"] == "test@example.com"

    def test_analyst_phone_masked(self):
        """analyst 角色手机号脱敏"""
        data = [
            {"id": 1, "phone_number": "13812345678"},
        ]
        masked_data, masked_cols = masking_service.mask_query_result(
            data, ["id", "phone_number"], role="analyst"
        )
        assert "phone_number" in masked_cols
        assert masked_data[0]["phone_number"] == "138****5678"

    def test_viewer_phone_masked(self):
        """viewer 角色手机号脱敏"""
        data = [
            {"id": 1, "phone_number": "13998765432"},
        ]
        masked_data, masked_cols = masking_service.mask_query_result(
            data, ["id", "phone_number"], role="viewer"
        )
        assert "phone_number" in masked_cols
        assert masked_data[0]["phone_number"] == "139****5432"

    def test_iccid_masked_except_last4(self):
        """ICCID 只显示后4位"""
        data = [
            {"iccid": "8986012345678901234"},
        ]
        masked_data, masked_cols = masking_service.mask_query_result(
            data, ["iccid"], role="analyst"
        )
        assert "iccid" in masked_cols
        assert masked_data[0]["iccid"].endswith("1234")
        assert "*" in masked_data[0]["iccid"]

    def test_imsi_fully_masked(self):
        """IMSI 全脱敏"""
        data = [
            {"imsi": "460001234567890"},
        ]
        masked_data, masked_cols = masking_service.mask_query_result(
            data, ["imsi"], role="analyst"
        )
        assert "imsi" in masked_cols
        assert masked_data[0]["imsi"] == "*" * 15

    def test_email_masked_for_analyst(self):
        """analyst 角色邮箱脱敏"""
        data = [
            {"email": "zhangsan@example.com"},
        ]
        masked_data, masked_cols = masking_service.mask_query_result(
            data, ["email"], role="analyst"
        )
        assert "email" in masked_cols
        assert masked_data[0]["email"] == "z***@example.com"

    def test_multiple_columns_masked(self):
        """多列同时脱敏"""
        data = [
            {
                "id": 1,
                "phone_number": "13812345678",
                "email": "test@example.com",
                "iccid": "8986012345678901234",
                "imsi": "460001234567890",
            },
        ]
        masked_data, masked_cols = masking_service.mask_query_result(
            data, ["id", "phone_number", "email", "iccid", "imsi"], role="analyst"
        )
        assert "phone_number" in masked_cols
        assert "email" in masked_cols
        assert "iccid" in masked_cols
        assert "imsi" in masked_cols
        assert "id" not in masked_cols  # id 不脱敏

    def test_activation_code_masked(self):
        """activation_code 全脱敏"""
        data = [
            {"activation_code": "LPA:1$example.com$ACT123456"},
        ]
        masked_data, masked_cols = masking_service.mask_query_result(
            data, ["activation_code"], role="analyst"
        )
        assert "activation_code" in masked_cols
        assert "*" in str(masked_data[0]["activation_code"])

    def test_non_sensitive_column_not_masked(self):
        """非敏感列不脱敏"""
        data = [
            {"id": 1, "region": "北京", "status": "active"},
        ]
        masked_data, masked_cols = masking_service.mask_query_result(
            data, ["id", "region", "status"], role="analyst"
        )
        assert len(masked_cols) == 0
        assert masked_data[0]["region"] == "北京"
        assert masked_data[0]["status"] == "active"

    def test_null_values_not_affected(self):
        """NULL 值不受脱敏影响"""
        data = [
            {"id": 1, "phone_number": None, "email": None},
        ]
        masked_data, masked_cols = masking_service.mask_query_result(
            data, ["id", "phone_number", "email"], role="analyst"
        )
        assert masked_data[0]["phone_number"] is None
        assert masked_data[0]["email"] is None


class TestContentDetection:
    """内容检测脱敏测试"""

    def test_phone_in_text_masked(self):
        """文本中的手机号被脱敏"""
        text = "联系电话：13812345678，请回复"
        result = masking_service.mask_content(text)
        assert "138****5678" in result
        assert "13812345678" not in result

    def test_email_in_text_masked(self):
        """文本中的邮箱被脱敏"""
        text = "发送到 test@example.com 即可"
        result = masking_service.mask_content(text)
        assert "t***@example.com" in result
        assert "test@example.com" not in result

    def test_normal_text_not_affected(self):
        """普通文本不受影响"""
        text = "这是一个普通文本，没有敏感信息"
        result = masking_service.mask_content(text)
        assert result == text
