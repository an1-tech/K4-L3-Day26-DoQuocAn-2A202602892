# Kết quả kiểm tra bài cá nhân

Đỗ Quốc An — MSSV 2A202602892. Bundle: DoQuocAn-2A202602892.
Ngày kiểm tra: 14/10/2026. Báo cáo này thay bản trước khi hoàn thiện.

## Môi trường, conformance và integrity

- Windows Python 3.12.10; world df8c55dabb35: 12.375 trang.
- SHA256 ZIP khớp checksum; không có truth.json trong world học viên.
- Bộ test công khai nguyên bản trên Ubuntu WSL / Python 3.12.15:
  **4.609 passed, 7 skipped**, không deselect hoặc monkeypatch kit.
- Lần chạy gộp công khai + 24 kiểm tra bổ sung: **4.633 passed, 7 skipped**.
- Sau đó thêm kiểm tra body bị tráo, replica sai, số liệu bị rút lại:
  **27 kiểm tra bổ sung pass** trên source mới nhất.
  **46 test liên quan prosecutor/G-KEY pass** khi kiểm tra lại.
- Windows có lỗi msvcrt.open trong probe kit; không sửa kit để lách test.
  Môi trường Linux riêng: ../.day26-wsl-test/.
- G-KEY và scanner import/call của kit.submit: PASS.
- git diff HEAD -- kit bots fixtures không có thay đổi.
- Bundle được kiểm tra CRC, từng hash nguồn và 39 hash kit.

## Gateway và guardrails

Gateway kiểm tra đầu vào, route cố định revision, card/skill/aud,
ctx.act và scopes, lease, If-Match đã quan sát, key ghi mới, mask,
retry cần sửa, rate window và reserve cho các vòng sau.
Chọn revision không tự chứng minh freshness.

Một Gateway và một HardMode dùng chung 10 vòng, thực thi MCP trên world thật:
query(title,body,1 row)=4 + get_frame(title,body)=4 + provenance(etag)=1.
Credit cuối mỗi vòng: **[91,82,73,64,55,46,37,28,19,10]**.
Tổng chi phí **90/100**. Kiểm tra giá tắt riêng hai cơ chế lỗi có seed,
không thay đổi giá và không thay thế trận đấu arena đầy đủ.

16 kiểm tra runner trước đó vẫn pass. 27 kiểm tra bổ sung bao phủ:
partial/continuation đúng và sai, required fields/type, câu hỏi sạch,
mâu thuẫn, unit/scale, private row, retry/rate/route, A2A signed admission,
ngân sách chung, luồng MockBroker thật, body bị tráo và envelope sai replica.
Chặn I/O trong hai entry point thuần và kiểm tra deadline cũng pass.
Lần đo cuối: gateway 0,096 ms; prosecutor chậm nhất 4,483 ms trên bộ mẫu.

GuardedAgent kiểm tra observation trước khi model đọc và ANSWER trước khi ghi.
Cần truyền ASK gốc, learner tin cậy và world nếu môi trường cung cấp.
Xem agent/README.md. Nếu arena dùng kit.loop.Agent trực tiếp, wrapper không
được tự gọi; cần xác nhận cách harness giảng viên tích hợp lớp trả lời.

## Deck và prosecutor

Deck: 14 lá = 10 attacks + 4 blanks; 4 MCP, 4 A2A, 2 gateway; đủ 9 lớp.
Lineup: 9 attacks + 1 blank. Mọi attack dùng gateway.denied theo hướng dẫn.
Validator trên world thật: **0 FAIL, 1 WARN** về lethality band.
Các lựa chọn và mutation đã chỉnh: deck/STRATEGY.md.

Cả 17 hook đã được nối vào chứng cứ. Trên 40 labelled fixtures:
**34 verified / 34 lỗi; precision=recall=F1=1.0**.
False, unproven, rejected, errors, timeouts: **0**.
Không đọc label/fixture_id trong detector, không đọc truth.json.
eval/world_facts.py chỉ chứa drift index công khai và checksum của world.
Tối đa 4 claims / 1 mỗi family; dedup theo causal event; argument <=400 ký tự.

Chọn bỏ trường hợp đã deny nhưng không có bằng chứng execution và failure
chỉ dựa vào forwarding khi card yêu cầu pin/crosscheck/refusal. Không dùng
row được đánh dấu corrupt làm chứng cứ sự thật. Số liệu đã bị rút lại không
bị coi là khẳng định mới. Các lựa chọn thận trọng này có thể bỏ sót lỗi.
Các detector ngữ nghĩa vẫn là heuristic, không bảo đảm 100% trên trace mới.

## Spar và UI

Tham số --as all --seed 1, bots tập công khai:

| Bot | HP bài cá nhân | HP bot | Vòng | FALSE của prosecutor cá nhân |
|---|---:|---:|---:|---:|
| rookie | 100 | 0 | 6 | 0 |
| operator | 100 | 0 | 5 | 0 |
| adversary | 72 | 0 | 8 | 0 |

Riêng adversary --as prosecutor --seed 1: **21–0**, 8 vòng, **0 FALSE**
qua kiểm tra claim_outcome trong nhật ký đã lưu.

UI gốc được serve/render bằng Edge headless, không sửa HTML kit.
Trạng thái cuối vòng 8: HP 72–0; đã đọc ô QUESTION/ACTION/ANSWER/REFEREE.
Tóm tắt: eval/UI_REVIEW.json; ảnh: runs/ui-review-final.png.

spar.py tự dựng plan và ANSWER, không chạy GuardedAgent/MockBroker thật.
Replay rookie còn 2 lần UNCITED. UI adversary cuối có citation /999 bị
chấm fabricated_citation. Không dùng dữ liệu do spar dựng để chứng minh
runner đúng. Luồng runner/broker thật được kiểm tra riêng ở test bổ sung.
Spar reset credits mỗi exchange và có nhánh bỏ qua target mutation;
HP/credits của nó không chứng minh lethality trong arena đầy đủ.

## Sáu checkpoint chuẩn bị nộp

1. Bộ test công khai pass trên Linux, không bỏ chọn test.
2. Validator không FAIL.
3. kit/, bots/, fixtures/ không đổi.
4. G-KEY và scanner import/call đạt.
5. Bundle tạo bằng kit.submit; CRC và các hash đối chiếu với source.
6. K4-DAY26-DoQuocAn-2A202602892/ chứa bundle mới và TEAMMATES.md
   ghi đúng Đỗ Quốc An — 2A202602892, bài làm cá nhân.

Chưa gửi bài đến nơi nộp; hướng dẫn vẫn ghi TODO.
Chưa xác minh lethality band bằng arena đầy đủ hoặc cách harness gọi runner.
Khả năng tự trả lời phần lý thuyết của người học không thể kiểm tra bằng code.
