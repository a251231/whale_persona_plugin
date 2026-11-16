"""Nov 12–16 上游更新回归测试套件（DSH v0.9.3/v1.6.1/v0.3.2/v0.1.84 & Diana #1544/#1555/#1557/#1563/#1577/#1583）。"""

import asyncio
import base64
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

_REAL_ASYNC_CLIENT = httpx.AsyncClient

try:
    import test_review_regressions as harness
except ImportError:
    from tests import test_review_regressions as harness

from components.event_listeners import diana_features as df
from components.event_listeners import whale_listener as wl


class Nov16UpstreamSyncTests(harness.ListenerHarness):
    # ------------------------------------------------------------------
    # 1. dsh-feiyu-mood v0.9.3: 8th mood dim `angry` (45-min fast decay & 对事不对人)
    # ------------------------------------------------------------------
    async def test_mood_angry_dimension_fast_decay_and_prompt_rule(self):
        dim_keys = [k for k, _ in wl.MOOD_DIMS]
        self.assertEqual(
            dim_keys,
            ["happy", "calm", "sweet", "heartache", "worry", "sad", "jealous", "angry"],
        )
        self.assertIn("angry", wl.MOOD_FAST_DECAY_DIMS)
        self.assertIn("只对「事」不对「人」", wl.MOOD_JUDGE_SYSTEM)
        self.assertIn('"angry":0', wl.MOOD_JUDGE_SYSTEM)

        u = self.listener._get_user_state("wxid_mood_user")
        self.assertIn("angry", u["dims"])

        # Set both `angry` (45m half-life) and `worry` (90m half-life) to 80, aged 45 minutes
        now_ms = int(time.time() * 1000)
        u["mood"] = 70
        u["dims"]["angry"] = 80
        u["dims"]["worry"] = 80
        u["lastDecayAt"] = now_ms - 45 * 60 * 1000

        self.listener._apply_mood_decay(u)
        # After 45 minutes: `angry` (~1 half-life) -> ~40; `worry` (0.5 half-life) -> ~57
        self.assertAlmostEqual(u["dims"]["angry"], 40, delta=2)
        self.assertGreater(u["dims"]["worry"], 53)
        self.assertLess(u["dims"]["angry"], u["dims"]["worry"] - 10)

    # ------------------------------------------------------------------
    # 2. dsh-speak-up v0.3.2: closed `<speak-up>...</speak-up>` prompt & outbound strip
    # ------------------------------------------------------------------
    async def test_speak_up_uses_closed_block_and_strips_echoed_tags(self):
        u = self.listener._get_user_state("wxid_su_user")
        u["lastChatAt"] = int((time.time() - 7200) * 1000)
        u["lastSpeakUpAt"] = 0
        u["speakUpCountSinceReply"] = 0

        self._patch(df, "send_wcf_text_or_image", AsyncMock(return_value=True))
        self.model_reply = "<speak-up>你在忙啥呢，半天没动静</speak-up>"

        txt, _ = await self.listener._trigger_speak_up_for_user("wxid_su_user", manual=True)
        self.assertTrue(self.requests)
        sent_user_prompt = json.loads(self.requests[-1].content.decode("utf-8"))["messages"][-1]["content"]
        self.assertIn("<speak-up>", sent_user_prompt)
        self.assertIn("</speak-up>", sent_user_prompt)
        self.assertIn("这个块里的内容是你自己心里冒出来的念头", sent_user_prompt)
        self.assertIsNotNone(txt)
        self.assertNotIn("<speak-up>", txt)
        self.assertNotIn("</speak-up>", txt)
        self.assertIn("你在忙啥呢", txt)

    # ------------------------------------------------------------------
    # 3. dsh-fish-memory v1.6.1: `stable_profile` & high-importance memories preserved
    # ------------------------------------------------------------------
    async def test_memory_recall_never_filters_out_stable_profile_or_high_importance(self):
        u = self.listener._get_user_state("wxid_mem_user")
        u["stable_profile"] = [
            {"text": "对花生严重过敏绝对不能碰", "category": "stable_profile", "importance": 1},
        ]
        # Crowd `facts` with 10 ordinary low-overlap items
        u["facts"] = [f"以前随口提过的普通日常小事编号第{i}条记录" for i in range(10)]
        u["events"] = [
            {"date": "2026-10-01", "text": "普通闲聊记录一", "ts": 1700000000000.0},
            {"date": "2026-10-02", "text": "普通闲聊记录二", "ts": 1700000000000.0},
            {"date": "2026-10-03", "text": "普通闲聊记录三", "ts": 1700000000000.0},
            {"date": "2026-10-04", "text": "普通闲聊记录四", "ts": 1700000000000.0},
            {"date": "2026-09-01", "text": "双方约定每年冬至一起吃饺子", "category": "promise", "importance": 1, "ts": 1690000000000.0},
        ]

        recalled = self.listener._recall_memories(u, "今晚点什么外卖好呢")
        self.assertIn("对花生严重过敏绝对不能碰", recalled)
        self.assertIn("双方约定每年冬至一起吃饺子", recalled)

    # ------------------------------------------------------------------
    # 4. dsh-meme v0.1.84 & Diana #1544 & Diana #1577
    # ------------------------------------------------------------------
    async def test_send_meme_tool_forbids_bracket_markers_and_sticker_gist_truncation(self):
        desc = wl.SEND_MEME_TOOL["function"]["description"]
        self.assertIn("[表情:", desc)
        self.assertTrue("严禁" in desc or "不要" in desc or "禁止" in desc)

        raw_long = "一只圆滚滚的蓝白小鲸鱼正抱着大碗米饭疯狂扒拉\n\n眼神发光充满了干饭人的喜悦" + ("哈" * 160)
        gist = df.truncate_sticker_history_gist(raw_long, max_chars=120)
        self.assertNotIn("\n", gist)
        self.assertLessEqual(len(gist), 121)

    async def test_tool_loop_injects_interim_sent_reminder_before_followup_completion(self):
        self._patch(df, "search_web_parallel", AsyncMock(return_value="今日上海晴，22度"))
        first_tool_turn = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "我先帮你瞅一眼今天上海的天气哈",
                        "tool_calls": [
                            {
                                "id": "call_weather_1",
                                "type": "function",
                                "function": {
                                    "name": "web_search",
                                    "arguments": json.dumps({"queries": ["上海今天天气"]}, ensure_ascii=False),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"total_tokens": 10},
        }
        second_turn = {
            "choices": [{"message": {"content": "查到啦，上海今天大晴天22度，出门挺舒服"}}],
            "usage": {"total_tokens": 12},
        }
        queued = [first_tool_turn, second_turn]

        def tool_respond(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            if req.url.path.endswith("/chat/completions") and queued:
                return httpx.Response(200, json=queued.pop(0))
            return httpx.Response(200, json=second_turn)

        self._patch(
            httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(tool_respond), **kwargs),
        )

        from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
        from types import SimpleNamespace

        ctx = SimpleNamespace(
            event=SimpleNamespace(
                message_chain=MessageChain([Plain(text="帮我查下今天上海天气")]),
                sender_id="wxid_tool_u",
                launcher_id="wxid_tool_u",
                source_platform_object={},
                reply_message_chain=None,
            ),
            query_id=None,
            is_prevented_default=lambda: False,
            prevent_default=lambda: None,
        )
        self.requests.clear()
        await self.listener._handle_chat(ctx, is_group=False)
        self.assertGreaterEqual(len(self.requests), 2)
        second_req = json.loads(self.requests[1].content.decode("utf-8"))
        dumped_msgs = json.dumps(second_req["messages"], ensure_ascii=False)
        self.assertIn("这一轮你已经发出去的话", dumped_msgs)
        self.assertIn("我先帮你瞅一眼今天上海的天气哈", dumped_msgs)

    # ------------------------------------------------------------------
    # 5. Diana #1563 & #1583: Bilibili / Douyin Live Room & Cover Timeout Preservation
    # ------------------------------------------------------------------
    async def test_bilibili_and_douyin_live_room_parsing_and_cover_timeout_preservation(self):
        self.assertEqual(df.bilibili_live_room_id("https://live.bilibili.com/blanc/21452505?broadcast_type=0"), "21452505")
        self.assertEqual(
            df.douyin_live_target("https://webcast.amemv.com/douyin/webcast/reflow/73182938471?u_code=abc"),
            ("73182938471", ""),
        )
        self.assertEqual(df.douyin_live_target("https://live.douyin.com/88990011"), ("", "88990011"))

        def mock_handler(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if "api.live.bilibili.com/room/v1/Room/get_info" in u:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "room_id": 21452505,
                            "title": "深夜敲代码陪伴直播",
                            "live_status": 1,
                            "user_cover": "https://i0.hdslb.com/bfs/live/cover_timeout.jpg",
                            "online": 12890,
                            "parent_area_name": "知识",
                            "area_name": "野生技术协会",
                        },
                    },
                )
            if "api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room" in u:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"info": {"uname": "技术肥鱼UP"}}},
                )
            if "webcast.amemv.com/webcast/room/reflow/info" in u:
                return httpx.Response(
                    200,
                    json={
                        "status_code": 0,
                        "data": {
                            "room": {
                                "id_str": "73182938471",
                                "status": 2,
                                "title": "干饭人夜宵专场",
                                "user_count": 4321,
                                "owner": {"nickname": "小鲸鱼吃播"},
                                "cover": {"url_list": ["https://p3.douyinpic.com/cover_timeout.jpg"]},
                            }
                        },
                    },
                )
            if "cover_timeout.jpg" in u:
                raise httpx.ReadTimeout("Simulated media download timeout")
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)

        def _client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return _REAL_ASYNC_CLIENT(*args, **kwargs)

        with patch.object(df, "_is_safe_public_url", return_value=True), patch.object(
            df.httpx, "AsyncClient", side_effect=_client_factory
        ):
            bili_txt, bili_cover = await df.extract_and_parse_link(
                "来看看 https://live.bilibili.com/21452505 播了！"
            )
            self.assertIsNotNone(bili_txt)
            self.assertIsNone(bili_cover)  # Timed out, yet metadata is preserved!
            self.assertIn("[B站直播] 深夜敲代码陪伴直播", bili_txt)
            self.assertIn("主播：技术肥鱼UP", bili_txt)
            self.assertIn("状态：直播中", bili_txt)
            self.assertIn("分区：知识 · 野生技术协会", bili_txt)
            self.assertIn("人气：12890", bili_txt)

            dy_txt, dy_cover = await df.extract_and_parse_link(
                "https://webcast.amemv.com/douyin/webcast/reflow/73182938471"
            )
            self.assertIsNotNone(dy_txt)
            self.assertIsNone(dy_cover)
            self.assertIn("[抖音直播] 干饭人夜宵专场", dy_txt)
            self.assertIn("主播：小鲸鱼吃播", dy_txt)
            self.assertIn("状态：直播中", dy_txt)
            self.assertIn("人气：4321", dy_txt)

    # ------------------------------------------------------------------
    # 6. Diana #1555 & #1557: DeepSeek Tools API (TTS / ASR / Search / Fetch / OCR)
    # ------------------------------------------------------------------
    async def test_deepseek_tool_endpoints_tts_asr_search_fetch_and_ocr(self):
        self.assertEqual(
            df.deepseek_tool_endpoint("https://api.deepseek.com", "tts"),
            "https://api.deepseek.com/v1/tools/tts",
        )
        self.assertEqual(
            df.deepseek_tool_endpoint("https://api.deepseek.com/v1/", "asr"),
            "https://api.deepseek.com/v1/tools/asr",
        )
        self.assertEqual(
            df.deepseek_tool_endpoint("https://api.deepseek.com/v1/tools/ocr", "ocr"),
            "https://api.deepseek.com/v1/tools/ocr",
        )

        seen_paths: list[str] = []

        def ds_handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen_paths.append(path)
            auth = request.headers.get("Authorization", "")
            self.assertEqual(auth, "Bearer sk-ds-unit")
            body_bytes = request.content

            if path == "/v1/tools/tts":
                payload = json.loads(body_bytes.decode("utf-8"))
                self.assertNotIn("model", payload)
                self.assertEqual(payload["voice_id"], "stella")
                # Return raw 4-byte PCM to verify automatic 44-byte RIFF WAV wrapping
                return httpx.Response(200, content=b"\x01\x00\x02\x00")
            if path == "/v1/tools/asr":
                self.assertIn(b'name="file"', body_bytes)
                self.assertNotIn(b'name="model"', body_bytes)
                return httpx.Response(200, json={"request_id": "r1", "text": " 语音识别成功啦 ", "duration_ms": 2100})
            if path == "/v1/tools/search":
                payload = json.loads(body_bytes.decode("utf-8"))
                self.assertEqual(payload["query"], "DeepSeek工具接口")
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "title": "DeepSeek Tools Guide",
                                "url": "https://api-docs.deepseek.com/guides/tools",
                                "snippet": "short",
                                "summary": "DeepSeek 现已支持搜索、网页读取与语音工具接口",
                            }
                        ]
                    },
                )
            if path == "/v1/tools/fetch":
                payload = json.loads(body_bytes.decode("utf-8"))
                return httpx.Response(
                    200,
                    json={"title": "测试反爬页面", "content": "# 正文标题\n这是通过 DeepSeek Fetch 拿到的完整正文内容，超过六十个字符以便验证读取成功。"},
                )
            if path == "/v1/tools/ocr":
                self.assertIn(b"text_recognition", body_bytes)
                raw_ocr = (
                    "\n\n"
                    + json.dumps(
                        {
                            "pages": [
                                {
                                    "index": 0,
                                    "text": "<｜ref｜>扫描件第一页标题<｜/ref｜><｜det｜>[[10, 20, 300, 40]]<｜/det｜>\n合同正文第一条款",
                                    "finish_reason": "stop",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                )
                return httpx.Response(200, content=raw_ocr.encode("utf-8"))
            return httpx.Response(404)

        transport = httpx.MockTransport(ds_handler)

        def _client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return _REAL_ASYNC_CLIENT(*args, **kwargs)

        with patch.object(df.httpx, "AsyncClient", side_effect=_client_factory):
            # (a) TTS
            wav_bytes = await df.synthesize_tts_via_deepseek("你好呀", "sk-ds-unit")
            self.assertIsNotNone(wav_bytes)
            self.assertTrue(wav_bytes.startswith(b"RIFF"))
            self.assertEqual(len(wav_bytes), 44 + 4)

            # (b) ASR (pass RIFF WAV bytes directly)
            fake_wav_b64 = base64.b64encode(df.wrap_pcm24k_to_wav(b"\x00\x00" * 100, 16000)).decode("ascii")
            asr_txt = await df.transcribe_audio_via_deepseek(fake_wav_b64, "sk-ds-unit")
            self.assertEqual(asr_txt, "语音识别成功啦")

            # (c) Search fallback when primary search returns empty
            with patch.object(df, "_search_single_query", AsyncMock(return_value="")):
                srch = await df.search_web_parallel(
                    ["DeepSeek工具接口"],
                    deepseek_api_key="sk-ds-unit",
                    deepseek_base_url="https://api.deepseek.com",
                )
                self.assertIn("DeepSeek 现已支持搜索、网页读取与语音工具接口", srch)
                self.assertNotIn("short", srch)

            # (d) Fetch SSRF protection: loopback URL must NEVER call /v1/tools/fetch!
            seen_paths.clear()
            blocked_res = await df.fetch_webpage_via_deepseek("http://127.0.0.1:8080/admin", "sk-ds-unit")
            self.assertEqual(seen_paths, [])
            self.assertTrue(not blocked_res or "安全限制" in blocked_res or "失败" in blocked_res)

            # Public URL fetch succeeds
            with patch.object(df, "_is_safe_public_url", return_value=True):
                fetched_txt = await df.fetch_webpage_via_deepseek("https://example.com/article", "sk-ds-unit")
                self.assertIn("测试反爬页面", fetched_txt)
                self.assertIn("DeepSeek Fetch", fetched_txt)

            # (e) OCR layout token cleanup + scanned PDF async fallback
            cleaned = df.clean_deepseek_ocr_text("<|ref|>发票抬头<|/ref|><|box|>[[1,2,3,4]]<|/box|>\n金额：100元")
            self.assertEqual(cleaned, "发票抬头\n金额：100元")

            fake_pdf_b64 = base64.b64encode(b"%PDF-1.4 scanned empty page").decode("ascii")
            scanned_xml = (
                f"<msg><appmsg><type>6</type><title>scan_contract.pdf</title>"
                f"<appattach><totallen>27</totallen><fileext>pdf</fileext></appattach>"
                f"</appmsg><file_b64>{fake_pdf_b64}</file_b64></msg>"
            )
            with patch.object(df, "parse_wechat_file_xml", return_value=("scan_contract.pdf", "[文件内容为空或为扫描件图片]")):
                fname, parsed_doc = await df.parse_wechat_file_xml_async(
                    scanned_xml,
                    deepseek_api_key="sk-ds-unit",
                    deepseek_base_url="https://api.deepseek.com",
                )
                self.assertEqual(fname, "scan_contract.pdf")
                self.assertIn("扫描件第一页标题", parsed_doc)
                self.assertIn("合同正文第一条款", parsed_doc)
                self.assertNotIn("det", parsed_doc)


if __name__ == "__main__":
    unittest.main()
