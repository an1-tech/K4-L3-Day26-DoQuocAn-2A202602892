# Agent cá nhân — Đỗ Quốc An, 2A202602892

- `gateway.py`: quyết định thuần, kiểm tra đầu vào, route theo revision
  anchor, card/skill/aud, quyền ctx.act, scope, lease, tiền đề ghi,
  idempotency, mask, retry cần sửa, rate window và ngân sách chung.
- `strategy.py`: giá từ TOOL_SPECS; 9 credit mỗi vòng chuẩn, 90 cho 10 vòng.
  Drift không tự chứng minh canonical mới hơn. Xác định bản mới hơn cần
  bằng chứng so sánh; không chọn chỉ vì tên replica.
- `guardrails.py`: citation, injection, riêng tư, số/đơn vị, kiểu và trường
  bắt buộc, partial và mâu thuẫn. Các quy tắc không thay thế oracle sự thật.
- `runner.py`: GuardedAgent kế thừa kit.loop.Agent; kiểm tra observation
  trước khi model đọc và ANSWER trước khi môi trường ghi nhận. Nếu có world
  tin cậy, đối chiếu body/title MCP và metadata peer với corpus frozen.
- `broker_adapter.py`: đổi observation sang role=tool cho MockBroker,
  chuyển lease đã nhận sang get_frame, đổi prose cuối sang ANSWER JSON.
  Chỉ lấy citation model thực sự viết, không tự tạo citation.
- `prompt.md`: chính sách chồng lên grammar kit, 4 lượt / 20 giây,
  100 credit chung cả duel, quy tắc dẫn nguồn và xử lý mâu thuẫn.
- `telemetry.py`: log riêng, không dùng làm bằng chứng công tố.

## Tích hợp

```python
from agent import GuardedAgent, BrokerAdapter
runner = GuardedAgent(
    BrokerAdapter(mock_broker), arena_environment,
    expected_act=trusted_context.act,
    ask=original_card["ask"], task=task_text,
    world=readonly_world,
    seen_token_ids=duel_seen_tokens,
)
result = runner.run()
```

Phải truyền ASK gốc để thực thi ask.require và learner từ ngữ cảnh xác thực.
Môi trường vẫn sở hữu dispatch, mint/verify delegation, trace và metering.
Gateway không thực thi tool hoặc mint credential. Runner không thay sandbox.
Nếu arena gọi kit.loop.Agent trực tiếp, wrapper không tự được gọi; cần xác
nhận cách harness giảng viên tích hợp lớp trả lời. Không sửa kit để gắn nó.

## Kiểm tra trên Windows

```powershell
& .\.venv\Scripts\python.exe -X utf8 -B -m eval.check_runner
& .\.venv\Scripts\python.exe -X utf8 -B -m unittest eval.test_completion -v
```

Kết quả thực tế và các giới hạn: eval/RESULTS.md.
