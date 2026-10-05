# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Nguyễn Minh Tuấn  **MSSV:** 2A202602420  **Ngày:** 05/10/2026

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu phải khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md`.

**Cấu hình và nguồn số liệu**

| | |
| --- | --- |
| Model | chat `gemini:gemini-3.1-flash-lite`, embedding `gemini:gemini-embedding-2` (lý do đổi model: xem "Vấn đề gặp phải") |
| Tham số | `top_k=3`, `chunk_size=800`, 176 chunk |
| `ket_qua_benchmark_kg.txt` | GraphRAG dùng **ontology tự thiết kế** (mặc định), KG 211 node / 443 cạnh. Lệnh: `python bench_kg.py --judge` |
| `ket_qua_benchmark_kg.hint.txt` | GraphRAG dùng **ontology gợi ý** (baseline cho bonus), KG 201 node / 388 cạnh. Lệnh: `KG_ONTOLOGY=hint python bench_kg.py --judge --out ket_qua_benchmark_kg.hint.txt` |

- Hai lần chạy cùng model, cùng embedding và cùng cách sắp thứ tự dữ kiện trong KG-3.
- KG-3 của ontology tự thiết kế còn có thay đổi truy vấn riêng (xem `ONTOLOGY.md` đầu file và mục 7), nên chênh lệch Graph giữa hai file là của ontology **cộng** KG-3 đi kèm.
- Phía Flat RAG ra cùng recall/judge/token/USD ở hai lần chạy; chỉ thời gian khác (2.53 s và 2.40 s).
- Mọi Cypher trong báo cáo chạy trên graph của đúng lần chạy được ghi kèm.
- Số dữ kiện `context()` (khác với số liệu benchmark) đo bằng `doc_id` chọn tay và được ghi rõ ở chỗ dùng.

## 1. Chi phí (10 điểm)

Dán 2 bảng `Indexing` và `Querying` từ `ket_qua_benchmark_kg.txt`:

```
Chat model: gemini:gemini-3.1-flash-lite | Embedding: gemini:gemini-embedding-2 | top_k=3 | chunk_size=800 | chunks=176 | KG: 211 nodes / 443 rels

== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176         0        0   0.00000    101.0
graph       196     37059     5430   0.01741    157.8

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.58   1.33      657       59   0.00025     2.40
graph       1.00   2.00     1430      126   0.00055     6.86
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0.00000 | 0.01741 | không chia được (Flat = 0, xem ghi chú) |
| Indexing giây | 101.0 | 157.8 | ×1.56 |
| Mỗi câu: USD | 0.00025 | 0.00055 | ×2.2 |
| Mỗi câu: giây | 2.40 | 6.86 | ×2.9 (trung bình); ×1.8 nếu dùng trung vị (2.36 → 4.20) |
| Mỗi câu: in_tok | 657 | 1430 | ×2.2 |

**Ghi chú:**
- **Vì sao Flat indexing = 0.** API embedding của Gemini (endpoint tương thích OpenAI) không trả về số token, nên `MeteredLLM.embed` ghi 0 token và $0. Indexing chỉ so được theo **giây**.
- **Trung vị độ trễ**, tính từ các dòng `Per question`:
  - Flat: 1.95, 2.21, 2.25, 2.46, 2.47, 3.05 → 2.36 s.
  - Graph: 1.82, 2.11, 3.52, 4.88, 4.95, 23.88 → 4.20 s.

  Trung bình của Graph bị một mẫu Q2 (23.88 s) kéo lên (xem E4).

**Chi phí tăng thêm đến từ đâu?**

**Indexing.** GraphRAG = vector index của Flat + dựng KG (`graph_index = flat_index + kg_build` trong `bench_kg.py`). Phần tăng thêm là:
- 196 − 176 = **20 lần gọi LLM** trích xuất 20 bài báo;
- 37,059 token vào + 5,430 token ra = **$0.01741**;
- 157.8 − 101.0 = **56.8 s**.

Phía luật (18 Điều, 99 khoản, 246 cạnh `MENTIONS` trong đó 239 cạnh có ngưỡng khối lượng) trích bằng regex nên tốn 0 token.

**Mỗi câu hỏi.** Chi phí tăng do prompt dài hơn: thêm 1430 − 657 = **773 token vào** mỗi câu. Phần thêm này gồm dữ kiện về người, vụ việc và khoản luật.
- Token vào: 773 × $0.25/1M ≈ $0.00019
- Token ra (+67 token): 67 × $1.50/1M ≈ $0.00010
- Tổng ≈ **$0.00030/câu**, khớp với 0.00055 − 0.00025.

**Hiệu quả của ontology tự thiết kế.** Với ontology gợi ý (`ket_qua_benchmark_kg.hint.txt`), GraphRAG cần 2678 token/câu và $0.00088/câu. Ontology mới cùng KG-3 viết cho nó **giảm 47% token mỗi câu**, vì KG-3 chỉ lấy luật theo tội của đúng người được hỏi và đúng khoản áp dụng, thay vì mọi khoản của mọi tội trong vụ.

**Điểm hòa vốn.** Với N câu hỏi, chi phí tăng thêm của GraphRAG là $0.01741 + $0.00030·N.
- Chi phí dựng KG bằng tiền chênh lệch của khoảng **58 câu hỏi** (0.01741 / 0.00030).
- Sau khoảng 58 câu, phần đắt nhất là prompt dài hơn chứ không còn là indexing.
- Đổi lại, trên 3 câu cross-kb, recall trung bình tăng từ 0.38 lên 1.00.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa | Định nghĩa "tiền chất" nằm gọn trong một chunk (Điều 2 Luật PCMT); graph không thêm gì (gọi `context()` với `doc_id` chọn tay `pcmt-dieu-2` trả về 0 dữ kiện) |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa | Hai bị cáo án tử hình nằm gọn trong một bài báo về vụ 36kg |
| Q3 | cross-kb | 0.00 / 0 | 1.00 / 2 | Graph | Flat chỉ trả lời "Không đủ thông tin."; Graph đi `Lê Minh Thành -INVOLVED_IN{charge}-> Crime <-DEFINES- Điều 251 -HAS_CLAUSE-> khoản 1` |
| Q4 | cross-kb | 0.33 / 1 | 1.00 / 2 | Graph | Flat không có chunk Điều 255; Graph lấy tội riêng của Hoàng Nato rồi tới khoản có `is_max_clause` (khoản 4, "tù chung thân") |
| Q5 | cross-kb-multi-hop | 0.80 / 2 | 1.00 / 2 | Graph | Flat tình cờ lấy được chunk khoản 4 nhưng không nêu số Điều; Graph chọn khoản 4 Điều 250 từ ngưỡng `MENTIONS {min_g: 100}` so với `INVOLVES {amount_g: 9600}` |
| Q6 | aggregation | 0.33 / 1 | 1.00 / 2 | Graph | Flat top-3 chunk chỉ phủ vụ Cái Quang Huy; Graph tra mọi `Case -INVOLVES-> MDMA` trên toàn graph, ra đủ 3 vụ không trùng kèm tên đối tượng |

**Quy luật:**
- **Câu nằm gọn trong một KB** (Q1, Q2): **hòa**. GraphRAG tốn gấp khoảng 2 lần tiền và chậm hơn mà không thêm gì.
- **Câu xuyên 2 KB** (Q3–Q5): GraphRAG thắng.
  - Recall trung bình 0.38 → 1.00; judge 1.0 → 2.0.
  - Không chunk nào chứa cả tên bị cáo lẫn khung hình phạt.
  - Q5 còn cần so số (khối lượng với ngưỡng), việc mà vector search không làm được.
- **Câu tổng hợp** (Q6): GraphRAG thắng **chỉ khi** KG-3 duyệt toàn graph (`Case -INVOLVES-> Substance`) thay vì chỉ các vụ trong top-k, **và** graph không trùng vụ.
  - Với ontology gợi ý, Q6 chỉ đạt recall 0.67 vì liệt kê 5 "vụ" cho 3 vụ thật (E3).

## 3. Phân tích lỗi (20 điểm)

### Lỗi E2: Thiếu ngữ cảnh luật — sai mức phạt tối đa dù graph có đủ Điều luật

**Hiện tượng:** với ontology gợi ý, GraphRAG trả lời Q4 là không xác định được mức phạt tối đa, dù graph có đủ 5 khoản của Điều 255.

**Bằng chứng:** Q4, GraphRAG, `ket_qua_benchmark_kg.hint.txt` — recall 0.67, judge 1:

> "**Mức phạt tù tối đa:** Theo [Điều 255 BLHS - Tội tổ chức sử dụng trái phép chất ma túy], dữ kiện chỉ cung cấp thông tin về khoản 1 với mức phạt tù từ 02 năm đến 07 năm. Đối với các khoản khác của Điều 255, dữ kiện không cung cấp thông tin chi tiết về khung hình phạt, do đó **không đủ thông tin** để xác định mức phạt tù tối đa"

Regex của ontology gợi ý (`parse_law_article` → `substances` từng khoản), chạy lại cho Điều 255:

```
khoản 1 []  phạt tù từ 02 năm đến 07 năm
khoản 2 []  phạt tù từ 07 năm đến 15 năm
khoản 3 []  phạt tù từ 15 năm đến 20 năm
khoản 4 []  phạt tù 20 năm hoặc tù chung thân
khoản 5 []  phạt tiền từ 50.000.000 đồng đến 500.000.000 …
```

Gọi `context()` cho Q4 trên graph ontology gợi ý, với `doc_id` chọn tay là hai bài về Hoàng Nato (`news-100260920221957595`, `news-100260922111804786`): kết quả có 36 dữ kiện, gồm khoản 1–4 của Điều 249, khoản 1–4 của Điều 251, nhưng **chỉ khoản 1** của Điều 255.

**Nguyên nhân:** do **thiết kế ontology** kết hợp với **Cypher KG-3**.
- KG-3 chỉ giữ khoản 1 và các khoản `MENTIONS` một chất mà vụ `INVOLVES`.
- Điều 255 tăng khung theo số người và tình tiết, không theo chất, nên không khoản nào `MENTIONS` chất, và bộ lọc loại hết khoản 2–4.
- Ontology gợi ý không có thuộc tính nào cho biết khoản nào là khung cao nhất.
- Ngoài ra, KG-3 lấy luật theo **mọi** tội của vụ (vụ Hoàng Nato `CHARGED_WITH` 3 tội), nên prompt bị lấp đầy bởi Điều 249 và 251 không liên quan.

**Đề xuất sửa (đã làm trong ontology tự thiết kế):**
- Thêm `Clause.severity` (tử hình 100, chung thân 50, còn lại là số năm tù tối đa) và `is_max_clause` cho đúng 1 khoản mỗi Điều. KG-3 luôn lấy khung cơ bản + khung cao nhất.
- Với người được nêu tên trong câu hỏi, KG-3 đi theo `INVOLVED_IN.charge` (tội riêng của người đó) thay vì mọi tội của vụ.

Kết quả trên graph mới:

```cypher
MATCH (p:Person)-[r:INVOLVED_IN]->(:Case) WHERE 'Hoàng Nato' IN p.aliases
MATCH (c:Crime {name: r.charge})<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause {is_max_clause: true})
RETURN p.name, r.charge, a.id, cl.number, cl.penalty;
```

```
Dương Minh Tuấn | tổ chức sử dụng trái phép chất ma túy | Điều 255 BLHS | 4 | phạt tù 20 năm hoặc tù chung thân
```

- Q4 GraphRAG trong `ket_qua_benchmark_kg.txt` đạt recall 1.00, judge 2: "mức phạt tù tối đa là 20 năm hoặc tù chung thân (theo khoản 4 của Điều luật)".
- Context Q4 (cùng `doc_id` chọn tay) giảm từ 36 xuống 7 dữ kiện.
- *Đánh đổi:* mỗi câu hỏi luôn có thêm 1 dòng khung cao nhất, kể cả khi câu hỏi không hỏi mức tối đa.

### Lỗi E3: Trùng thực thể — một vụ ngoài đời thành nhiều node `Case`

**Hiện tượng:** trên graph ontology gợi ý, cùng một vụ án thành nhiều node `Case`:
- chuyên án Hoàng Nato: 4 node;
- vụ Cái Quang Huy: 2 node.

Chỗ trùng lọt cả vào câu trả lời.

**Bằng chứng (graph của lần chạy `ket_qua_benchmark_kg.hint.txt`):**

```cypher
MATCH (p:Person)-[:INVOLVED_IN]->(k:Case) WHERE 'Hoàng Nato' IN p.aliases
RETURN p.name, k.name, k.doc_id ORDER BY k.name;
```

```
Dương Minh Tuấn | Vụ bắt giữ 126 người liên quan 8 đường dây ma túy tại TP.HCM                          | news-100260920221957595
Dương Minh Tuấn | Vụ triệt phá 8 đường dây ma túy của 'Hoàng Nato' tại TP.HCM                             | news-100260925144412498
Dương Minh Tuấn | Vụ triệt phá 8 đường dây ma túy tại TP.HCM liên quan TikToker Phannhibeauty và Hoàng Nato | news-100260922111804786
Dương Minh Tuấn | Vụ tổ chức sử dụng ma túy của 'Hoàng Nato' và Phannhibeauty                              | news-100260924095400982
```

```cypher
MATCH (p:Person {name:'Cái Quang Huy'})-[:INVOLVED_IN]->(k:Case) RETURN k.name, k.doc_id;
```

```
Vụ vận chuyển ma túy qua sân bay Nội Bài của Cái Quang Huy    | news-100260918080821054
Vụ vận chuyển ma túy từ Đức về Việt Nam qua sân bay Nội Bài   | news-100260917203001265
```

Tổng số `Case` là **17**. Q6, GraphRAG, `ket_qua_benchmark_kg.hint.txt` (recall 0.67) liệt kê 5 "vụ" cho 3 vụ thật:

> "1. **Vụ vận chuyển ma túy từ Đức về Việt Nam qua sân bay Nội Bài** … 2. **Vụ vận chuyển ma túy qua sân bay Nội Bài của Cái Quang Huy** … 3. **Vụ tổ chức sử dụng ma túy tại Sầm Sơn và tàng trữ ma túy tại Viện Pháp y tâm thần Trung ương** … 4. **Vụ án sai phạm tại Viện Pháp y tâm thần Trung ương** … 5. **Vụ góp tiền mua ma túy tại Hà Nội**"

**Nguyên nhân:** lỗi nằm ở 2 bước.
1. **Thiết kế ontology:** `Case` khóa theo `name` do LLM tự đặt, mà mỗi bài báo đặt tên vụ một kiểu nên `MERGE` không gộp được. Mỗi `Case` cũng chỉ giữ một `doc_id`, nên ontology không biểu diễn được "một vụ, nhiều bài báo".
2. **Crawl:** đoạn cuối của 3 bài là teaser tóm tắt **bài khác**, và LLM trích chúng thành vụ riêng:
   - `news-100260918080821054.md` dòng 92 (Cái Quang Huy);
   - `news-100260927182621527.md` dòng 36 ("126 người");
   - `news-100260918220613301.md` dòng 42 ("Đức Cộng").

**Đề xuất sửa (đã làm trong ontology tự thiết kế):**
- Thêm node `NewsArticle {doc_id}` và cạnh `REPORTS`; `Case` không còn gắn với một bài.
- `Case` được gộp nếu có chung bị cáo/bị can/nghi phạm (họ tên ≥ 2 chữ); `Person` được gộp theo bí danh.
- Prompt trích xuất yêu cầu bỏ qua đoạn tin liên quan ở cuối bài.

Kết quả (graph của lần chạy `ket_qua_benchmark_kg.txt`):

```cypher
MATCH (p:Person)-[:INVOLVED_IN]->(k:Case) WHERE 'Hoàng Nato' IN p.aliases
RETURN p.name, k.name, [(n:NewsArticle)-[:REPORTS]->(k) | n.doc_id] AS reported_by;
```

```
Dương Minh Tuấn | Vụ triệt phá 8 đường dây ma túy liên quan 'Hoàng Nato' tại TP.HCM | ['news-100260925144412498', 'news-100260924095400982', 'news-100260922111804786', 'news-100260920221957595']
```

- Tổng `Case` giảm từ 17 xuống 10.
- Vụ Viện Pháp y tâm thần cũng thành 1 node có 2 `REPORTS`.
- Q6 GraphRAG liệt kê đúng 3 vụ: recall 1.00, judge 2.
- *Đánh đổi:*
  - Có thể gộp nhầm hai vụ thật sự khác nhau của cùng một người (tái phạm).
  - Mỗi lần thêm `Case` tốn 1–2 truy vấn tra cứu trong Neo4j; không tốn thêm token.
  - Vẫn nên sửa tận gốc ở `scripts/crawl_drug_corpus.py` (bỏ khối "tin liên quan").

### Lỗi E4: Phép đo sai — độ trễ trung bình và điểm judge không phản ánh đúng

**Hiện tượng:** có 3 chỗ phép đo không phản ánh đúng thực tế.
1. **Một mẫu chi phối trung bình độ trễ.** GraphRAG trung bình 6.86 s, trong khi 5/6 câu chỉ mất 1.82–4.95 s.
2. **Judge quá dễ dãi.** Q5 Flat có judge 2 ("đúng và đủ") dù thiếu số Điều luật mà đáp án chuẩn yêu cầu.
3. **Recall đánh giá thấp một câu trả lời đúng.** Q6 GraphRAG với ontology gợi ý chỉ được recall 0.67 dù đã nêu đủ 3 vụ, vì gọi vụ Lê Minh Thành bằng tên vụ.

**Bằng chứng:**

Các dòng `Per question` của `ket_qua_benchmark_kg.txt`:

```
--- Q2 [single-hop-news] graph recall=1.00 judge=2 23.88s
(5 câu Graph còn lại: 4.88s, 2.11s, 1.82s, 3.52s, 4.95s)
```

Q5, Flat, `ket_qua_benchmark_kg.txt` (recall 0.80, judge 2):

> "Với khối lượng hơn 9,6kg MDMA (vượt mức 100g quy định tại điểm b khoản 4), bị cáo thuộc trường hợp quy định tại **khoản 4** của điều luật."

Câu trả lời không nêu "Điều 250", trong khi đáp án chuẩn ghi rõ "Điều 250 BLHS"; recall đúng khi trừ điểm còn judge thì không. Q6 GraphRAG, `ket_qua_benchmark_kg.hint.txt`: recall 0.67 nhưng judge 2. Câu trả lời nêu "Vụ góp tiền mua ma túy tại Hà Nội" (đúng là vụ nhóm Lê Minh Thành) nhưng không có chuỗi "Lê Minh Thành".

**Nguyên nhân:** lỗi nằm ở **chính phép đo**.
- `MeteredLLM.chat` trong `src/llm.py` bắt đầu đếm giờ **trước** vòng retry. Khi Gemini trả 429/503, code `time.sleep(…)` rồi gọi lại, và thời gian ngủ bị tính vào `seconds`. Trong phiên làm bài, key Gemini miễn phí thực sự chạm giới hạn 429 nhiều lần (xem "Vấn đề gặp phải"), nên mẫu 23.88 s nhiều khả năng là thời gian chờ retry chứ không phải độ trễ pipeline.
- Mỗi pipeline chỉ có 6 mẫu nên trung bình rất nhạy với ngoại lệ.
- Judge là LLM chấm 1 lần, không có rubric bắt buộc số Điều.
- `keyword_recall` chỉ khớp chuỗi nguyên văn.

**Đề xuất sửa:** không sửa `bench_kg.py`; đây là đề xuất cho một phiên bản benchmark sau.
- Báo cáo **trung vị** độ trễ (×1.8 thay vì ×2.9).
- Đo thời gian bên trong `_call_chat` để không tính thời gian chờ retry.
  - *Đánh đổi:* cần sửa `src/llm.py`, và số liệu cũ không so trực tiếp được.
- Judge có rubric ("thiếu số Điều luật → tối đa 1"), chạy 3 lần lấy trung vị.
  - *Đánh đổi:* chi phí chấm tăng gấp 3.
- `must_include` cho phép nhóm từ thay thế cho mỗi ý. Ví dụ ý "vụ Lê Minh Thành" chấp nhận `["Lê Minh Thành", "góp tiền mua ma túy"]`.

## 4. Kết luận (5 điểm)

Khi nào nên dùng KG, khi nào Flat RAG là đủ? Dẫn số liệu ở mục 1–2.

> **Nên dùng KG khi câu hỏi phải ghép thông tin từ nhiều nguồn qua một thực thể chung, hoặc phải so sánh số liệu với quy tắc.**
> - Ba câu cross-kb (Q3–Q5): recall Flat 0.00 / 0.33 / 0.80 → Graph 1.00 / 1.00 / 1.00.
> - Câu tổng hợp Q6: 0.33 → 1.00.
> - Lý do: không chunk nào chứa cả "Lê Minh Thành 36 tháng" lẫn "Điều 251 khoản 1". Việc chọn khoản 4 cho "9,6kg MDMA" cũng cần so khối lượng với ngưỡng 100 g, mà ngưỡng đó chỉ có trong graph dưới dạng số (`min_g`).
> - Cái giá phải trả:
>   - dựng KG một lần: $0.01741, 20 lần gọi LLM, +57 s;
>   - mỗi câu hỏi: tiền ×2.2 ($0.00025 → $0.00055), input token ×2.2;
>   - độ trễ trung vị ×1.8.
>
> **Flat RAG là đủ khi đáp án nằm gọn trong một đoạn văn** (Q1, Q2). Hai bên đều đạt 1.00 / 2, nên GraphRAG chỉ tốn thêm khoảng 2 lần tiền.
>
> **Thiết kế ontology (cùng truy vấn KG-3 đi kèm) quyết định KG có đáng tiền hay không.** Cùng dữ liệu và cùng model, ontology gợi ý chỉ đạt recall 0.89 / judge 1.83 với 2678 token/câu. Ontology tự thiết kế đạt 1.00 / 2.00 với 1430 token/câu, vì có khung cao nhất, ngưỡng khối lượng, gộp vụ trùng, và lấy luật theo tội của đúng người.
>
> **Điều kiện cụ thể để KG đáng tiền:**
> 1. Dữ liệu có thực thể dùng chung giữa các nguồn (tội danh, chất), và phía có cấu trúc trích được bằng regex nên gần như miễn phí.
> 2. Phần lớn câu hỏi là xuyên nguồn, nhiều bước hoặc tổng hợp.
> 3. Số câu hỏi đủ lớn để chi phí dựng KG ($0.017, bằng tiền chênh lệch của khoảng 58 câu) không đáng kể.
>
> Nếu đa số câu hỏi là single-hop thì nên dùng Flat RAG, hoặc chỉ gọi graph khi câu hỏi nêu tên người, Điều luật hay chất.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.06s

$ python bench_kg.py --check
(TODO: chạy SAU KHI chụp xong 3 ảnh Neo4j, rồi dán output vào đây.
 --check xóa graph và dựng lại chỉ với luật + 1 bài báo.)
```
PS C:\Users\NITRO\ProjectLab1\K4-DAY19-NguyenMinhTuan-2A202602420> python bench_kg.py --check        
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = gemini:gemini-3.1-flash-lite | embedding = gemini:gemini-embedding-2
[OK] KG-2 build_graph: 148 node / 368 cạnh, đường xuyên 2 KB dài 3 cạnh
[OK] KG-3 context: 6 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00136. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.

Ảnh Neo4j (chụp trên graph của lần chạy `ket_qua_benchmark_kg.txt`): `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.

- Truy vấn Q-B theo ontology tự thiết kế:
  ```cypher
  MATCH p=(:NewsArticle)-[:REPORTS]->(:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(:Article) RETURN p LIMIT 25
  ```
  Đường đi từ node bài báo, qua node cầu nối `Crime`, tới node Điều luật.
- Người đã chọn cho `kg_my_case.png`: **Trần Thanh Tuấn** (vụ mua bán hơn 36kg ma túy tại TP.HCM, án tử hình → Điều 251 và Điều 255 BLHS).

## Vấn đề gặp phải (không tính điểm)

Lỗi chưa giải quyết được: lệnh đã chạy, toàn bộ thông báo lỗi, những gì đã thử.

> 1. **Model mặc định không còn dùng được.** `python bench_kg.py --check` với model mặc định báo `openai.NotFoundError: Error code: 404 - … This model models/gemini-2.5-flash-lite is no longer available to new users …`. Đã chuyển sang `gemini-3.5-flash-lite`.
> 2. **Hết quota gói miễn phí của Gemini sau vài lần chạy benchmark trong ngày:**
>    - `429 … Quota exceeded for metric: generativelanguage.googleapis.com/embed_content_free_tier_requests, limit: 1000, model: gemini-embedding-1.0`
>    - `429 … generate_content_free_tier_requests, limit: 500, model: gemini-3.5-flash-lite`
>
>    Quota tính riêng cho từng model, nên đã đổi sang chat `gemini-3.1-flash-lite` (giá $0.25 / $1.50 cho mỗi 1M token theo trang giá Gemini API) và embedding `gemini-embedding-2`, rồi chạy lại **cả hai** benchmark với cùng cấu hình đó.
> 3. **Key OpenRouter chưa có credit.** Key được thêm vào sau trả `402 Insufficient credits`. Vì code tự chọn OpenRouter khi có key, đã đặt `LLM_PROVIDER=gemini` và `EMBEDDING_PROVIDER=gemini` trong `.env`.
> 4. **Embedding không có số liệu chi phí.** Endpoint embedding của Gemini không trả về số token, nên chi phí indexing của Flat RAG luôn hiển thị $0 và 0 token.
