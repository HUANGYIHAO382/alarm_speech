"""
API 查询模块 (Alarm API Client)
==================================
职责：
    - 纯网络层, 只负责发请求 / 拿数据
    - 不做任何展示、不触发任何语音
    - 调用方拿到的就是"干净的告警 dict 列表"或 None (失败)

后续如果服务端 API 变更, 只改本文件即可, 其它三个模块零感知。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import requests


@dataclass
class ApiResult:
    """统一的返回结构, 让上层不用判断各种 None。"""
    ok: bool
    data: list
    error: str = ""

    @classmethod
    def fail(cls, msg: str) -> "ApiResult":
        return cls(ok=False, data=[], error=msg)

    @classmethod
    def success(cls, data: list) -> "ApiResult":
        return cls(ok=True, data=data)


class AlarmApiClient:
    """
    封装"告警搜索"接口的三种典型用法:
      1) fetch_history(limit)          : 全量历史 (含已恢复)
      2) fetch_active_alarms(limit)    : 仅未处理/处理中 (用于轮询)
      3) query_by_id(alarm_id)         : 按唯一 id 反查单条 (用于确认是否恢复)
    """

    SEARCH_PATH = "/api/monitor/alarm/search"

    def __init__(self, host: str, token: str, timeout: float = 8.0):
        self.host = (host or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.timeout = timeout

    # ---------- 通用底层请求 ----------
    def _get(self, params: dict) -> ApiResult:
        if not self.host or not self.token:
            return ApiResult.fail("HOST 或 Token 为空")

        url = f"{self.host}{self.SEARCH_PATH}"
        headers = {"token": self.token}

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            if resp.status_code != 200:
                return ApiResult.fail(f"HTTP {resp.status_code}")
            payload = resp.json()
            if payload.get("code") != 0:
                return ApiResult.fail(payload.get("message") or "接口返回非 0")
            return ApiResult.success(payload.get("data", {}).get("list", []) or [])
        except requests.exceptions.RequestException as err:
            return ApiResult.fail(f"网络异常: {err}")
        except ValueError:
            return ApiResult.fail("响应非 JSON")

    # ---------- 业务接口 ----------
    def fetch_history(self, limit: int = 100) -> ApiResult:
        """全量历史 (按最近告警时间倒序), 含已恢复和未恢复。"""
        params = {
            "limit": limit,
            "offset": 0,
            "sort_unit_list": json.dumps(
                [{"column": "last_alarm_time", "sort": "desc"}]
            ),
        }
        return self._get(params)

    def fetch_active_alarms(self, limit: int = 50) -> ApiResult:
        """仅"未处理 / 处理中"的活跃告警, 用于定时轮询。"""
        params = {
            "limit": limit,
            "offset": 0,
            "search_unit_list": json.dumps([
                {"attr": "is_ignore", "search": [0], "operator": "="},
                {"attr": "status", "search": [0], "operator": "="},
            ]),
            "sort_unit_list": json.dumps(
                [{"column": "last_alarm_time", "sort": "desc"}]
            ),
        }
        return self._get(params)

    def query_by_id(self, alarm_id) -> Optional[dict]:
        """
        按唯一 id 反查单条告警, 不带 status 过滤,
        这样无论它已恢复(is_recover=1)还是被忽略, 都能查到。
        :return: 告警 dict, 找不到或失败返回 None
        """
        params = {
            "limit": 1,
            "offset": 0,
            "search_unit_list": json.dumps(
                [{"attr": "id", "search": [alarm_id], "operator": "="}]
            ),
        }
        result = self._get(params)
        if not result.ok or not result.data:
            return None
        return result.data[0]
