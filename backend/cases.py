"""
After-sales cases ("sự cố") for Bui Records.

Two ways a case is opened:
- the customer, from "Tài khoản": cancel an order that has not been delivered yet,
  or report a problem / ask for a return after it was delivered;
- the shop, from the admin page: any shipping, return, carrier or money incident.

Every case type carries a playbook (the steps to follow) and the actions that make
sense for it. Actions that touch the order (cancel, mark lost, receive the return,
send a replacement...) go through database.move_order, so stock always stays right.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone

import database

# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

GROUPS = [
    ("customer", "Khách hủy / trả hàng"),
    ("delivery", "Sự cố giao hàng"),
    ("return", "Sự cố hàng hoàn"),
    ("carrier", "Đơn vị vận chuyển"),
    ("finance", "Phí và bồi thường"),
]

# What each action does is in apply_action(). Labels are the button text.
ACTIONS = {
    "approve_cancel": "Duyệt hủy đơn",
    "mark_delivered": "Xác nhận khách đã nhận",
    "mark_failed": "Ghi nhận giao thất bại",
    "mark_lost": "Ghi nhận thất lạc",
    "receive_return": "Kho đã nhận hàng hoàn",
    "reship": "Gửi bù / gửi lại",
    "refund": "Hoàn tiền cho khách",
    "claim": "Gửi khiếu nại ĐVVC",
    "compensation": "Ghi nhận tiền bồi thường",
    "reject": "Từ chối yêu cầu",
    "resolve": "Đóng sự cố",
}

STATUSES = {
    "open": "Cần xử lý",
    "waiting_carrier": "Chờ ĐVVC",
    "waiting_customer": "Chờ khách",
    "resolved": "Đã xong",
    "rejected": "Đã từ chối",
}
OPEN_STATUSES = ("open", "waiting_carrier", "waiting_customer")
CUSTOMER_STATUS_LABELS = {
    "open": "Bụi đang xử lý",
    "waiting_carrier": "Bụi đang làm việc với đơn vị vận chuyển",
    "waiting_customer": "Bụi đang chờ bạn phản hồi",
    "resolved": "Đã xử lý xong",
    "rejected": "Không được chấp nhận",
}

# key: (group, label, needs_order, due_hours, actions, steps)
# due_hours = how long the shop gives itself for the first real action.
_T = {}


def _type(key, group, label, actions, steps, due_hours=24, needs_order=True):
    _T[key] = {
        "key": key, "group": group, "label": label, "needs_order": needs_order,
        "due_hours": due_hours, "actions": list(actions), "steps": list(steps),
    }


# ---- customer: cancel / return ------------------------------------------------
_type("cancel_request", "customer", "Khách hủy đơn trước khi giao",
      ["approve_cancel", "receive_return", "refund", "reject"], [
          "Kiểm tra đơn đã đóng gói hoặc đã bàn giao cho ĐVVC chưa.",
          "Chưa bàn giao: duyệt hủy, các bản đĩa được cộng lại kho ngay.",
          "Đã bàn giao: gọi ĐVVC yêu cầu dừng giao và chuyển hoàn, ghi mã yêu cầu vào ghi chú, rồi duyệt hủy (đơn chuyển sang Giao không thành công).",
          "Khách đã thanh toán trước: hoàn tiền ngay khi hủy, hoặc khi hàng hoàn về kho nếu đơn đã gửi.",
          "Trả lời khách kết quả và thời gian nhận lại tiền (nếu có).",
      ], due_hours=12)
_type("return_change_of_mind", "customer", "Khách trả hàng vì đổi ý / hết nhu cầu",
      ["receive_return", "refund", "reject"], [
          "Kiểm tra đơn còn trong hạn đổi trả (7 ngày kể từ khi giao).",
          "Đĩa phải còn nguyên seal, bìa không trầy móp; nếu không đạt, từ chối kèm lý do.",
          "Thống nhất với khách ai trả phí gửi về và số tiền sẽ hoàn.",
          "Gửi khách địa chỉ kho, xin mã vận đơn chiều về và ghi vào ghi chú.",
          "Nhận hàng, kiểm tra tình trạng, cộng kho rồi hoàn tiền.",
      ])
_type("wrong_item_sent", "customer", "Khách nhận sai đĩa",
      ["reship", "receive_return", "refund"], [
          "Xin ảnh đĩa khách nhận được và tem vận đơn.",
          "Đối chiếu với ảnh/video đóng gói để xác nhận shop gửi nhầm.",
          "Gửi đúng đĩa cho khách (gửi bù), shop chịu phí.",
          "Hướng dẫn khách gửi trả đĩa nhầm, cộng lại kho khi nhận được.",
      ])
_type("customer_other", "customer", "Khách báo vấn đề khác",
      ["reship", "receive_return", "refund", "claim", "reject"], [
          "Gọi hoặc nhắn khách để hiểu rõ vấn đề, xin ảnh/video nếu cần.",
          "Nếu thuộc một tình huống cụ thể, tạo sự cố đúng loại để theo đúng quy trình.",
          "Thống nhất cách xử lý với khách và ghi lại.",
      ])

# ---- delivery to the customer ---------------------------------------------------
_type("delivered_not_received", "delivery", "ĐVVC báo đã giao, khách nói chưa nhận",
      ["claim", "compensation", "reship", "refund", "mark_lost", "mark_delivered"], [
          "Xác nhận lại địa chỉ, số điện thoại với khách; nhờ khách hỏi người nhà, bảo vệ, hàng xóm.",
          "Yêu cầu ĐVVC cung cấp bằng chứng giao (ảnh, chữ ký, định vị) và mở khiếu nại.",
          "ĐVVC không chứng minh được: ghi nhận thất lạc, gửi lại hoặc hoàn tiền cho khách.",
          "Theo dõi khoản bồi thường của ĐVVC.",
          "Khách tìm thấy hàng: xác nhận đã nhận và đóng sự cố.",
      ], due_hours=48)
_type("wrong_address_delivery", "delivery", "Giao nhầm địa chỉ",
      ["claim", "compensation", "reship", "refund", "mark_lost"], [
          "So địa chỉ trên đơn với địa chỉ trên vận đơn để biết lỗi do shop, khách hay shipper.",
          "Lỗi nhập địa chỉ: sửa và yêu cầu ĐVVC chuyển tiếp hoặc giao lại.",
          "Shipper giao sai: yêu cầu ĐVVC thu hồi gói; không thu hồi được thì khiếu nại bồi thường.",
          "Báo khách tiến độ; gửi lại hoặc hoàn tiền nếu không lấy lại được hàng.",
      ])
_type("delivered_package_damaged", "delivery", "Khách nhận gói rách / móp",
      ["claim", "compensation", "refund", "reship", "receive_return"], [
          "Xin ảnh vỏ gói và video mở hộp.",
          "Kiểm tra đĩa bên trong: mặt đĩa, bìa, góc bìa.",
          "Đĩa nguyên vẹn: xin lỗi khách, ghi nhận, có thể tặng mã giảm giá.",
          "Đĩa bị ảnh hưởng: xử lý như sản phẩm vỡ/hỏng (gửi đĩa mới hoặc hoàn tiền).",
          "Khiếu nại ĐVVC kèm ảnh nếu có thiệt hại.",
      ])
_type("delivered_broken", "delivery", "Khách nhận sản phẩm vỡ / hỏng do vận chuyển",
      ["reship", "refund", "receive_return", "claim", "compensation"], [
          "Xin video mở hộp liền mạch, ảnh đĩa, bìa và vỏ gói.",
          "Xác nhận hư hỏng do vận chuyển (vỡ, cong, móp), không phải lỗi ép đĩa.",
          "Đề xuất với khách: gửi đĩa mới hoặc hoàn tiền.",
          "Giữ lại hoặc thu hồi đĩa hỏng nếu ĐVVC cần làm bằng chứng.",
          "Khiếu nại ĐVVC đòi bồi thường.",
      ])
_type("missing_items", "delivery", "Khách báo thiếu hàng",
      ["reship", "refund", "claim", "compensation"], [
          "Đối chiếu đơn với ảnh/video đóng gói và cân nặng lúc bàn giao.",
          "Xin video mở hộp của khách.",
          "Shop đóng thiếu: gửi bù phần thiếu.",
          "Nghi thất thoát trên đường (cân nặng lệch, tem bị bóc): khiếu nại ĐVVC.",
      ])
_type("empty_box", "delivery", "Khách báo nhận hộp rỗng",
      ["reship", "refund", "claim", "compensation"], [
          "Xin video mở hộp từ lúc gói còn nguyên tem.",
          "Đối chiếu cân nặng lúc bàn giao với cân nặng ĐVVC ghi nhận.",
          "Khiếu nại ĐVVC ngay (thất thoát trong quá trình vận chuyển).",
          "Đủ bằng chứng: gửi lại đĩa hoặc hoàn tiền cho khách.",
      ], due_hours=12)
_type("wrong_parcel", "delivery", "Khách nhận gói của người khác",
      ["claim", "compensation", "reship", "refund", "mark_lost"], [
          "Nhờ khách giữ nguyên gói, không mở thêm.",
          "Báo ĐVVC thu hồi gói nhầm và tìm gói của khách.",
          "Không tìm được gói của khách: gửi lại hoặc hoàn tiền, khiếu nại ĐVVC.",
      ])
_type("late_delivery", "delivery", "Quá hạn giao",
      ["mark_delivered", "mark_failed", "mark_lost", "claim", "refund"], [
          "Tra hành trình trên trang ĐVVC bằng mã vận đơn.",
          "Yêu cầu ĐVVC hẹn ngày giao cụ thể.",
          "Báo khách tình hình và ngày dự kiến.",
          "Quá lâu không giao được: khiếu nại, hỏi khách muốn chờ hay hủy.",
      ])
_type("stuck_tracking", "delivery", "Hành trình đứng yên nhiều ngày",
      ["mark_lost", "claim", "compensation", "reship", "refund"], [
          "Ghi lại mốc quét cuối cùng và bưu cục đang giữ hàng.",
          "Tạo yêu cầu tra soát với ĐVVC.",
          "Không cập nhật thêm sau vài ngày: xem như nghi thất lạc, khiếu nại.",
          "Báo khách và đề xuất gửi lại hoặc hoàn tiền nếu cần.",
      ])
_type("lost_outbound", "delivery", "Mất hàng chiều đi",
      ["mark_lost", "reship", "refund", "claim", "compensation"], [
          "Lấy xác nhận thất lạc của ĐVVC (văn bản hoặc mã khiếu nại).",
          "Ghi nhận đơn thất lạc (không cộng lại kho).",
          "Gửi lại đĩa mới hoặc hoàn tiền cho khách.",
          "Theo dõi bồi thường theo giá trị hàng đã khai.",
      ])
_type("delivered_not_updated", "delivery", "Khách đã nhận nhưng hệ thống chưa cập nhật",
      ["mark_delivered"], [
          "Xác nhận với khách đã nhận đủ hàng, đúng đĩa.",
          "Đối chiếu hành trình trên trang ĐVVC.",
          "Cập nhật đơn sang Đã giao.",
      ], due_hours=24)
_type("refused_not_contacted", "delivery", "Shipper báo khách từ chối, khách nói không được liên hệ",
      ["mark_failed", "reship", "receive_return", "claim", "refund"], [
          "Xin ĐVVC lịch sử cuộc gọi và ghi chú của shipper.",
          "Xác nhận lại với khách thời gian và địa chỉ nhận.",
          "Hàng chưa chuyển hoàn: yêu cầu ĐVVC giao lại.",
          "Hàng đã chuyển hoàn: hỏi khách có muốn gửi lại không.",
          "Phản ánh shipper nếu báo sai.",
      ])
_type("failed_customer", "delivery", "Giao thất bại do khách",
      ["mark_failed", "receive_return", "refund", "reship"], [
          "Liên hệ khách qua điện thoại và tin nhắn.",
          "Hẹn giao lại trong số lần ĐVVC cho phép.",
          "Khách từ chối nhận: cho chuyển hoàn, cộng kho khi hàng về.",
          "Đơn trả trước: hoàn tiền theo chính sách (có thể trừ phí ship hai chiều nếu đã thông báo).",
      ])
_type("failed_address", "delivery", "Giao thất bại do địa chỉ",
      ["mark_failed", "receive_return", "reship", "refund"], [
          "Xác nhận lại địa chỉ đầy đủ với khách (số nhà, ngõ, phường).",
          "Gửi địa chỉ mới cho ĐVVC để giao lại.",
          "Không liên hệ được khách: cho chuyển hoàn.",
      ])

# ---- returns coming back to the shop -------------------------------------------
_type("return_reported_not_received", "return", "Đơn hoàn đã báo \"đã trả\" nhưng kho chưa nhận",
      ["receive_return", "claim", "compensation", "mark_lost"], [
          "Kiểm tra hành trình chiều hoàn: ai ký nhận, ở đâu, lúc nào.",
          "Hỏi người nhận hàng tại kho (lễ tân, bảo vệ).",
          "Yêu cầu ĐVVC cung cấp biên nhận giao hoàn (chữ ký, ảnh).",
          "Không có bằng chứng: khiếu nại thất lạc hàng hoàn.",
      ])
_type("return_wrong_warehouse", "return", "Hàng hoàn bị giao về sai địa chỉ kho",
      ["receive_return", "claim", "compensation", "mark_lost"], [
          "Kiểm tra địa chỉ trả hàng đang khai báo trong tài khoản ĐVVC.",
          "Yêu cầu ĐVVC chuyển tiếp về đúng kho.",
          "Sửa địa chỉ trả hàng mặc định để không lặp lại.",
          "Không thu hồi được: khiếu nại bồi thường.",
      ])
_type("return_broken", "return", "Hàng hoàn về bị vỡ, hỏng",
      ["receive_return", "claim", "compensation"], [
          "Quay video mở gói hoàn ngay khi nhận, giữ nguyên vỏ gói.",
          "Chụp tem, mã vận đơn và đĩa hỏng; lập biên bản với shipper nếu kịp.",
          "Nhận hàng hoàn KHÔNG cộng kho phần hỏng.",
          "Khiếu nại ĐVVC sớm (hạn khiếu nại thường chỉ vài ngày).",
      ], due_hours=12)
_type("return_package_damaged", "return", "Hàng hoàn về bị rách, móp gói",
      ["receive_return", "claim", "compensation"], [
          "Quay video mở gói hoàn.",
          "Kiểm tra đĩa: nguyên vẹn thì nhận và cộng kho.",
          "Đĩa bị ảnh hưởng: nhận không cộng kho và xử lý như hàng hoàn vỡ/hỏng.",
          "Khiếu nại ĐVVC nếu có thiệt hại.",
      ])
_type("return_lost", "return", "Hàng hoàn bị mất trên đường trả",
      ["mark_lost", "claim", "compensation"], [
          "Lấy xác nhận thất lạc của ĐVVC.",
          "Ghi nhận đơn thất lạc (không cộng kho).",
          "Khiếu nại và theo dõi bồi thường.",
      ])
_type("return_empty_box", "return", "Hàng hoàn về là hộp rỗng",
      ["receive_return", "claim", "compensation"], [
          "Quay video mở gói từ lúc còn nguyên tem; ghi cân nặng thực tế.",
          "Lập biên bản với shipper nếu còn ở đó.",
          "Nhận hàng hoàn KHÔNG cộng kho.",
          "Khiếu nại ĐVVC; với đơn khách tự gửi trả, không hoàn tiền khi chưa rõ.",
      ], due_hours=12)
_type("return_wrong_item", "return", "Hàng hoàn về không phải hàng shop gửi đi",
      ["receive_return", "claim", "compensation", "refund", "reject"], [
          "Quay video mở gói; so sánh với ảnh đóng gói lúc gửi (đĩa, bìa, mã catalogue).",
          "Nhận hàng hoàn KHÔNG cộng kho.",
          "Hàng hoàn của ĐVVC: khiếu nại ĐVVC.",
          "Khách tự gửi trả: từ chối hoàn tiền kèm bằng chứng, đề nghị khách gửi đúng đĩa.",
      ])
_type("failed_return_not_received", "return", "Giao thất bại, chuyển hoàn nhưng kho chưa nhận",
      ["receive_return", "mark_lost", "claim", "compensation"], [
          "Kiểm tra trạng thái chuyển hoàn trên trang ĐVVC.",
          "Hỏi ĐVVC ngày dự kiến hàng về kho.",
          "Quá lâu chưa về: yêu cầu tra soát; nghi mất thì khiếu nại.",
      ], due_hours=72)
_type("return_status_inquiry", "return", "Hỏi tiến độ hàng hoàn",
      ["receive_return"], [
          "Tra mã vận đơn chiều hoàn.",
          "Hỏi ĐVVC ngày dự kiến về kho.",
          "Trả lời khách hoặc ghi lại kết quả.",
      ], due_hours=24)
_type("return_received_not_updated", "return", "Kho đã nhận hoàn nhưng hệ thống chưa cập nhật",
      ["receive_return", "refund"], [
          "Kiểm tra thực tế hàng hoàn trong kho.",
          "Cập nhật đơn sang Đã trả hàng (cộng kho nếu đĩa nguyên vẹn).",
          "Đơn trả trước: hoàn tiền cho khách nếu chưa hoàn.",
      ], due_hours=24)

# ---- carrier / shipper -------------------------------------------------------------
_type("shipper_damaged_lost", "carrier", "Shipper làm hỏng / thất lạc hàng",
      ["reship", "refund", "mark_lost", "claim", "compensation"], [
          "Thu thập bằng chứng: ảnh, video, biên bản.",
          "Khiếu nại ĐVVC kèm giá trị hàng.",
          "Xử lý cho khách: gửi lại hoặc hoàn tiền.",
      ])
_type("shipper_attitude", "carrier", "Thái độ shipper",
      ["claim"], [
          "Ghi lại thời gian, nội dung và bằng chứng (ghi âm, tin nhắn).",
          "Gửi phản ánh lên tổng đài hoặc kênh hỗ trợ của ĐVVC.",
          "Xin lỗi và cảm ơn khách đã báo.",
      ], due_hours=48)
_type("late_pickup", "carrier", "ĐVVC lấy hàng trễ",
      ["claim"], [
          "Ghi lại thời điểm tạo vận đơn và giờ hẹn lấy.",
          "Liên hệ bưu cục hoặc tổng đài ĐVVC.",
          "Đơn giao nhanh có nguy cơ trễ hạn: tự mang ra bưu cục hoặc đổi ĐVVC.",
          "Báo khách nếu đơn sẽ đến trễ.",
      ], due_hours=6)
_type("handed_not_scanned", "carrier", "Đã bàn giao nhưng chưa quét",
      ["claim", "compensation", "mark_lost"], [
          "Tìm biên bản bàn giao hoặc ảnh shipper nhận hàng.",
          "Liên hệ bưu cục yêu cầu quét nhận.",
          "Quá 1–2 ngày vẫn chưa có hành trình: khiếu nại thất lạc.",
      ], due_hours=24)

# ---- money ---------------------------------------------------------------------------
_type("compensation_tracking", "finance", "Theo dõi / khiếu nại khoản bồi thường",
      ["claim", "compensation"], [
          "Ghi mã khiếu nại và số tiền yêu cầu bồi thường.",
          "Theo dõi hạn phản hồi của ĐVVC.",
          "Đối chiếu khoản bồi thường trong kỳ đối soát.",
          "Thiếu hoặc chậm: khiếu nại lại.",
      ], due_hours=168, needs_order=False)
_type("shipping_fee_overcharged", "finance", "Phí ship bị thu cao bất thường",
      ["claim", "compensation"], [
          "Đối chiếu cân nặng, kích thước khai báo với số liệu ĐVVC ghi nhận.",
          "Đối chiếu với bảng giá đã thỏa thuận.",
          "Gửi yêu cầu điều chỉnh kèm ảnh cân và số tiền chênh lệch.",
      ], due_hours=72, needs_order=False)
_type("settlement_wrong", "finance", "Tiền quyết toán đơn sai",
      ["claim", "compensation"], [
          "Đối chiếu tiền thu hộ (COD), phí ship, phí hoàn trong bảng đối soát với đơn.",
          "Ghi số tiền chênh lệch.",
          "Gửi khiếu nại đối soát cho ĐVVC.",
          "Kiểm tra lại ở kỳ đối soát sau.",
      ], due_hours=72, needs_order=False)

TYPES = _T

# ---- what the customer can pick in "Tài khoản" ---------------------------------
CANCEL_REASONS = [
    ("no_need", "Hết nhu cầu"),
    ("changed_mind", "Không thích nữa, đổi ý"),
    ("wrong_order", "Đặt nhầm đĩa hoặc số lượng"),
    ("change_info", "Muốn đổi địa chỉ hoặc số điện thoại"),
    ("cheaper", "Tìm được nơi bán rẻ hơn"),
    ("too_slow", "Thời gian giao quá lâu"),
    ("other", "Lý do khác"),
]
# (key, label, case type, order statuses where it can be picked)
REPORT_REASONS = [
    ("broken", "Đĩa bị vỡ, cong vênh hoặc hỏng", "delivered_broken", ("completed",)),
    ("package_damaged", "Gói hàng bị rách, móp", "delivered_package_damaged", ("completed",)),
    ("missing", "Thiếu hàng so với đơn", "missing_items", ("completed",)),
    ("empty", "Nhận được hộp rỗng", "empty_box", ("completed",)),
    ("wrong_item", "Nhận sai đĩa", "wrong_item_sent", ("completed",)),
    ("someone_else", "Nhận nhầm gói của người khác", "wrong_parcel", ("completed",)),
    ("not_received", "Đơn báo đã giao nhưng tôi chưa nhận được", "delivered_not_received", ("completed", "shipping")),
    ("late", "Đơn giao quá lâu, hành trình không cập nhật", "late_delivery", ("shipping",)),
    ("change_of_mind", "Hết nhu cầu, muốn trả lại (đĩa còn nguyên seal)", "return_change_of_mind", ("completed",)),
    ("other", "Vấn đề khác", "customer_other", ("completed", "shipping")),
]
RETURN_WINDOW_DAYS = 7
PREPAID = ("bank", "momo", "zalopay", "card")
MAX_TEXT = 1000


def meta():
    """Everything the admin page needs to draw the case screens."""
    return {
        "groups": [{"key": k, "label": v} for k, v in GROUPS],
        "types": list(TYPES.values()),
        "actions": ACTIONS,
        "statuses": STATUSES,
    }


def reasons_payload():
    return {
        "cancel": [{"key": k, "label": v} for k, v in CANCEL_REASONS],
        "report": [{"key": k, "label": v, "statuses": list(s)} for k, v, _t, s in REPORT_REASONS],
        "return_window_days": RETURN_WINDOW_DAYS,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_sql(hours=0):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _new_code(conn):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "SC-" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not conn.execute("SELECT 1 FROM cases WHERE code = ?", (code,)).fetchone():
            return code


def _text(value, label, required=False, max_len=MAX_TEXT):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{label} không hợp lệ.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"Vui lòng nhập {label.lower()}.")
    if len(value) > max_len:
        raise ValueError(f"{label} quá dài (tối đa {max_len} ký tự).")
    return value


def _amount(value, label, allow_zero=False):
    if type(value) is not int or value < 0 or (value == 0 and not allow_zero) or value > 1_000_000_000:
        raise ValueError(f"{label} phải là số tiền hợp lệ.")
    return value


def money(n):
    return f"{n:,}".replace(",", ".") + "₫"


def add_event(conn, case_id, actor, kind, text, public=False):
    conn.execute(
        "INSERT INTO case_events (case_id, actor, kind, text, public) VALUES (?, ?, ?, ?, ?)",
        (case_id, actor, kind, text, 1 if public else 0),
    )
    conn.execute("UPDATE cases SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (case_id,))


def _set_status(conn, case, status):
    closed = status in ("resolved", "rejected")
    conn.execute(
        "UPDATE cases SET status = ?, closed_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, 1 if closed else 0, case["id"]),
    )


def _order_for(conn, case):
    if case["order_id"] is None:
        raise ValueError("Sự cố này không gắn với đơn hàng nào.")
    return conn.execute("SELECT * FROM orders WHERE id = ?", (case["order_id"],)).fetchone()


def open_case_for(conn, order_id, source=None):
    sql = f"SELECT * FROM cases WHERE order_id = ? AND status IN ({', '.join('?' * len(OPEN_STATUSES))})"
    args = [order_id, *OPEN_STATUSES]
    if source:
        sql += " AND source = ?"
        args.append(source)
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()


def create_case(conn, *, order, type_key, source, reason=None, detail="", actor="shop", first_event=None):
    """Insert a case inside the caller's transaction. Returns its id."""
    t = TYPES.get(type_key)
    if not t:
        raise ValueError("Loại sự cố không hợp lệ.")
    if t["needs_order"] and order is None:
        raise ValueError("Loại sự cố này cần mã đơn hàng.")
    code = _new_code(conn)
    cur = conn.execute(
        "INSERT INTO cases (code, order_id, type, source, reason, detail, due_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (code, order["id"] if order is not None else None, type_key, source, reason, detail,
         _now_sql(t["due_hours"])),
    )
    case_id = cur.lastrowid
    add_event(conn, case_id, actor, "created", first_event or f"Tạo sự cố: {t['label']}.")
    return case_id


def _tx(conn, fn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn()
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Customer requests
# ---------------------------------------------------------------------------

def customer_cancel(conn, order_no, user_id, reason, detail):
    """
    Customer cancels an order that has not been delivered.
      pending   -> cancelled at once (copies back in stock); a case is kept for the record
                   and stays open when money has to be refunded.
      confirmed -> request: the shop may have packed it already.
      shipping  -> request: the shop has to ask the carrier to stop and send it back.
    Returns the case code.
    """
    labels = dict(CANCEL_REASONS)
    if reason not in labels:
        raise ValueError("Vui lòng chọn lý do hủy đơn.")
    detail = _text(detail, "Mô tả", required=(reason == "other"))

    def run():
        order = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
        if not order or order["user_id"] != user_id:
            raise LookupError("Không tìm thấy đơn hàng.")
        if order["status"] not in ("pending", "confirmed", "shipping"):
            raise ValueError("Đơn này đã giao hoặc đã kết thúc, không thể hủy. Nếu đĩa có vấn đề, hãy chọn Báo sự cố.")
        if open_case_for(conn, order["id"], "customer"):
            raise ValueError("Bạn đã gửi một yêu cầu cho đơn này, Bụi đang xử lý.")
        said = f"Khách yêu cầu hủy đơn. Lý do: {labels[reason]}." + (f" Ghi chú: {detail}" if detail else "")
        case_id = create_case(conn, order=order, type_key="cancel_request", source="customer",
                              reason=reason, detail=detail, actor="customer", first_event=said)
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if order["status"] == "pending":
            database.move_order(conn, order, "cancelled")
            add_event(conn, case_id, "system", "order",
                      "Đơn chưa được xác nhận nên đã hủy ngay; các bản đĩa đã cộng lại kho.")
            if order["payment_method"] in PREPAID:
                add_event(conn, case_id, "system", "note",
                          f"Khách đã chọn thanh toán trước: cần hoàn {money(order['total'])}.")
                add_event(conn, case_id, "shop", "auto",
                          "Đơn đã được hủy. Bụi sẽ hoàn tiền cho bạn trong 1–3 ngày làm việc.", public=True)
            else:
                add_event(conn, case_id, "shop", "auto", "Đơn đã được hủy.", public=True)
                _set_status(conn, case, "resolved")
        else:
            add_event(conn, case_id, "shop", "auto",
                      "Bụi đã nhận yêu cầu hủy và sẽ phản hồi sớm"
                      + (" (đơn đã giao cho đơn vị vận chuyển, Bụi sẽ yêu cầu dừng giao)." if order["status"] == "shipping" else "."),
                      public=True)
        return case["code"]

    return _tx(conn, run)


def customer_report(conn, order_no, user_id, reason, detail):
    """Customer reports a delivery problem or asks to return. Creates the matching case type."""
    found = next((r for r in REPORT_REASONS if r[0] == reason), None)
    if not found:
        raise ValueError("Vui lòng chọn vấn đề bạn gặp.")
    key, label, type_key, statuses = found
    detail = _text(detail, "Mô tả", required=(key in ("other", "missing", "wrong_item")))

    def run():
        order = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
        if not order or order["user_id"] != user_id:
            raise LookupError("Không tìm thấy đơn hàng.")
        if order["status"] not in statuses:
            raise ValueError("Vấn đề này không áp dụng cho trạng thái hiện tại của đơn.")
        if order["status"] == "completed":
            since = order["updated_at"] or order["created_at"]
            limit = datetime.strptime(since, "%Y-%m-%d %H:%M:%S") + timedelta(days=RETURN_WINDOW_DAYS)
            if datetime.now(timezone.utc).replace(tzinfo=None) > limit:
                raise ValueError(f"Đơn đã giao quá {RETURN_WINDOW_DAYS} ngày. Vui lòng liên hệ Bụi để được hỗ trợ.")
        if open_case_for(conn, order["id"], "customer"):
            raise ValueError("Bạn đã gửi một yêu cầu cho đơn này, Bụi đang xử lý.")
        said = f"Khách báo: {label}." + (f" Mô tả: {detail}" if detail else "")
        case_id = create_case(conn, order=order, type_key=type_key, source="customer",
                              reason=key, detail=detail, actor="customer", first_event=said)
        add_event(conn, case_id, "shop", "auto",
                  "Bụi đã nhận thông tin. Nếu có, hãy chuẩn bị ảnh hoặc video mở hộp; "
                  "Bụi sẽ liên hệ qua số điện thoại của đơn trong 24 giờ.", public=True)
        return conn.execute("SELECT code FROM cases WHERE id = ?", (case_id,)).fetchone()["code"]

    return _tx(conn, run)


def customer_view(conn, order_id):
    """Cases of one order as the customer may see them (only public replies)."""
    rows = conn.execute("SELECT * FROM cases WHERE order_id = ? ORDER BY id DESC", (order_id,)).fetchall()
    reasons = dict(CANCEL_REASONS) | {k: v for k, v, _t, _s in REPORT_REASONS}
    out = []
    for c in rows:
        replies = conn.execute(
            "SELECT created_at, text FROM case_events WHERE case_id = ? AND public = 1 ORDER BY id DESC LIMIT 3",
            (c["id"],),
        ).fetchall()
        if c["source"] != "customer" and not replies:
            continue   # the shop's internal incident: nothing to show the customer yet
        out.append({
            "code": c["code"],
            "title": "Yêu cầu hủy đơn" if c["type"] == "cancel_request" else (reasons.get(c["reason"]) or TYPES[c["type"]]["label"]),
            "status": c["status"],
            "status_label": CUSTOMER_STATUS_LABELS.get(c["status"], c["status"]),
            "created_at": c["created_at"],
            "refunded": c["refunded"],
            "replies": [{"at": r["created_at"], "text": r["text"]} for r in replies],
        })
    return out


# ---------------------------------------------------------------------------
# Shop actions
# ---------------------------------------------------------------------------

def shop_create(conn, order_no, type_key, detail):
    detail = _text(detail, "Mô tả")
    t = TYPES.get(type_key)
    if not t:
        raise ValueError("Loại sự cố không hợp lệ.")

    def run():
        order = None
        if order_no:
            order = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
            if not order:
                raise ValueError("Không tìm thấy đơn hàng " + order_no + ".")
        case_id = create_case(conn, order=order, type_key=type_key, source="shop", detail=detail,
                              first_event=f"Tạo sự cố: {t['label']}." + (f" {detail}" if detail else ""))
        return conn.execute("SELECT code FROM cases WHERE id = ?", (case_id,)).fetchone()["code"]

    return _tx(conn, run)


def set_checklist(conn, code, done):
    t = conn.execute("SELECT type FROM cases WHERE code = ?", (code,)).fetchone()
    if not t:
        raise LookupError("Không tìm thấy sự cố.")
    steps = len(TYPES[t["type"]]["steps"])
    if not isinstance(done, list) or any(type(i) is not int or not 0 <= i < steps for i in done):
        raise ValueError("Danh sách bước không hợp lệ.")
    conn.execute("UPDATE cases SET checklist = ?, updated_at = CURRENT_TIMESTAMP WHERE code = ?",
                 (json.dumps(sorted(set(done))), code))
    conn.commit()


def apply_action(conn, code, action, params, new_order_no):
    """
    Run one shop action on a case, atomically. `params` is the JSON body.
    new_order_no(conn) makes an order number (used by reship).
    Returns a short Vietnamese summary for the toast.
    """
    params = params if isinstance(params, dict) else {}

    def run():
        case = conn.execute("SELECT * FROM cases WHERE code = ?", (code,)).fetchone()
        if not case:
            raise LookupError("Không tìm thấy sự cố.")
        t = TYPES[case["type"]]
        closed = case["status"] not in OPEN_STATUSES
        generic = ("note", "set_status", "reopen", "resolve", "reject")
        if action not in generic and action not in t["actions"]:
            raise ValueError("Thao tác này không dùng cho loại sự cố này.")
        if closed and action != "reopen" and action != "note":
            raise ValueError("Sự cố đã đóng. Mở lại để xử lý tiếp.")
        note = _text(params.get("note"), "Ghi chú")
        public = bool(params.get("public"))

        def log(text, kind="action"):
            add_event(conn, case["id"], "shop", kind, text + (f" Ghi chú: {note}" if note and kind != "reply" else ""))

        def reply(text):
            add_event(conn, case["id"], "shop", "reply", text, public=True)

        if action == "note":
            text = _text(params.get("text"), "Nội dung", required=True)
            add_event(conn, case["id"], "shop", "reply" if public else "note", text, public=public)
            return "Đã gửi cho khách." if public else "Đã thêm ghi chú."

        if action == "set_status":
            status = params.get("status")
            if status not in ("open", "waiting_carrier", "waiting_customer"):
                raise ValueError("Trạng thái không hợp lệ.")
            _set_status(conn, case, status)
            log(f"Chuyển sang: {STATUSES[status]}.", "status")
            return f"Đã chuyển sang {STATUSES[status]}."

        if action == "reopen":
            if not closed:
                raise ValueError("Sự cố đang mở.")
            _set_status(conn, case, "open")
            log("Mở lại sự cố.", "status")
            return "Đã mở lại sự cố."

        if action == "resolve":
            summary = _text(params.get("summary"), "Kết quả xử lý")
            _set_status(conn, case, "resolved")
            log("Đóng sự cố." + (f" Kết quả: {summary}" if summary else ""), "status")
            if summary and public:
                reply(summary)
            return "Đã đóng sự cố."

        if action == "reject":
            why = _text(params.get("reason"), "Lý do từ chối", required=True)
            _set_status(conn, case, "rejected")
            log("Từ chối yêu cầu.", "status")
            reply(why)
            return "Đã từ chối và gửi lý do cho khách."

        order = _order_for(conn, case) if action not in ("claim", "compensation") or case["order_id"] else None

        if action == "approve_cancel":
            if order["status"] in ("pending", "confirmed"):
                database.move_order(conn, order, "cancelled")
                log("Duyệt hủy: đơn đã hủy, các bản đĩa đã cộng lại kho.")
                prepaid = order["payment_method"] in PREPAID and case["refunded"] < order["total"]
                reply("Đơn của bạn đã được hủy." + (" Bụi sẽ hoàn tiền trong 1–3 ngày làm việc." if prepaid else ""))
                if not prepaid:
                    _set_status(conn, case, "resolved")
                return "Đã hủy đơn" + (", còn cần hoàn tiền." if prepaid else ".")
            if order["status"] == "shipping":
                database.move_order(conn, order, "failed")
                _set_status(conn, case, "waiting_carrier")
                log("Duyệt hủy khi đơn đang giao: đã yêu cầu ĐVVC dừng giao, đơn chuyển sang Giao không thành công và chờ hàng hoàn.")
                reply("Bụi đã yêu cầu đơn vị vận chuyển dừng giao và chuyển hoàn. Đơn sẽ được hủy khi hàng về kho"
                      + ("; Bụi sẽ hoàn tiền lúc đó." if order["payment_method"] in PREPAID else "."))
                return "Đơn chuyển sang Giao không thành công, chờ hàng hoàn."
            raise ValueError("Đơn không còn ở trạng thái có thể hủy.")

        if action == "mark_delivered":
            database.move_order(conn, order, "completed")
            log("Xác nhận khách đã nhận hàng; đơn chuyển sang Đã giao.")
            return "Đơn đã chuyển sang Đã giao."

        if action == "mark_failed":
            database.move_order(conn, order, "failed")
            log("Ghi nhận giao thất bại; đơn chuyển sang Giao không thành công.")
            return "Đơn chuyển sang Giao không thành công."

        if action == "mark_lost":
            database.move_order(conn, order, "lost")
            log("Ghi nhận thất lạc; đơn chuyển sang Thất lạc (không cộng kho).")
            return "Đơn chuyển sang Thất lạc."

        if action == "receive_return":
            restock = params.get("restock")
            if type(restock) is not bool:
                raise ValueError("Cho biết hàng hoàn về còn bán được hay không.")
            database.move_order(conn, order, "returned", restock=restock)
            log("Kho đã nhận hàng hoàn; " + ("đĩa nguyên vẹn, đã cộng lại kho." if restock else "đĩa không bán được, KHÔNG cộng kho."))
            if case["source"] == "customer":
                reply("Bụi đã nhận được hàng gửi về.")
            return "Đã nhận hàng hoàn" + (" và cộng kho." if restock else ", không cộng kho.")

        if action == "reship":
            items = params.get("items")
            if not isinstance(items, list) or not items or len(items) > 50:
                raise ValueError("Chọn ít nhất một đĩa để gửi.")
            originals = {str(r["item_id"]): r for r in conn.execute(
                "SELECT item_id, item_type, title, artist, qty FROM order_items WHERE order_id = ?", (order["id"],))}
            picked = []
            for it in items:
                if not isinstance(it, dict):
                    raise ValueError("Danh sách đĩa không hợp lệ.")
                item_id, qty = str(it.get("item_id")), it.get("qty")
                if item_id not in originals or type(qty) is not int or not 1 <= qty <= originals[item_id]["qty"]:
                    raise ValueError("Số bản gửi bù phải từ 1 đến số bản trong đơn gốc.")
                picked.append((originals[item_id], qty))
            no = new_order_no(conn)
            cur = conn.execute(
                """
                INSERT INTO orders (order_no, name, phone, address, city, note, shipping_method, shipping_fee,
                                    payment_method, subtotal, total, status, user_id, parent_order_id,
                                    created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 0, 'confirmed', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (no, order["name"], order["phone"], order["address"], order["city"],
                 f"Gửi bù cho đơn {order['order_no']} (sự cố {case['code']}). {note}".strip(),
                 order["shipping_method"], order["payment_method"], order["user_id"], order["id"]),
            )
            for orig, qty in picked:
                conn.execute(
                    "INSERT INTO order_items (order_id, item_id, item_type, title, artist, unit_price, qty) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (cur.lastrowid, orig["item_id"], orig["item_type"], orig["title"], orig["artist"], qty),
                )
                database.take_stock(conn, orig["item_id"], qty, orig["title"])
            what = ", ".join(f"{o['title']} x{q}" for o, q in picked)
            log(f"Tạo đơn gửi bù {no} (0₫, đã trừ kho): {what}.")
            reply(f"Bụi sẽ gửi lại cho bạn: {what}. Mã đơn gửi bù: {no}.")
            return f"Đã tạo đơn gửi bù {no}; đơn nằm trong nhóm Cần gửi."

        if action == "refund":
            amount = _amount(params.get("amount"), "Số tiền hoàn")
            left = order["total"] - order["refunded"]
            if amount > left:
                raise ValueError(f"Chỉ còn có thể hoàn tối đa {money(max(0, left))} cho đơn này.")
            method = _text(params.get("method"), "Cách hoàn tiền", max_len=120)
            conn.execute("UPDATE orders SET refunded = refunded + ? WHERE id = ?", (amount, order["id"]))
            conn.execute("UPDATE cases SET refunded = refunded + ? WHERE id = ?", (amount, case["id"]))
            log(f"Hoàn {money(amount)} cho khách" + (f" qua {method}." if method else "."))
            reply(f"Bụi đã hoàn {money(amount)} cho bạn" + (f" qua {method}." if method else "."))
            return f"Đã ghi nhận hoàn {money(amount)}."

        if action == "claim":
            amount = _amount(params.get("amount", 0), "Số tiền yêu cầu bồi thường", allow_zero=True)
            ref = _text(params.get("carrier_ref"), "Mã khiếu nại", max_len=80)
            conn.execute(
                "UPDATE cases SET claimed = ?, carrier_ref = COALESCE(NULLIF(?, ''), carrier_ref), due_at = ? WHERE id = ?",
                (amount, ref, _now_sql(24 * 7), case["id"]),
            )
            _set_status(conn, case, "waiting_carrier")
            log("Đã gửi khiếu nại ĐVVC" + (f", mã {ref}" if ref else "") +
                (f", yêu cầu bồi thường {money(amount)}" if amount else "") + ". Hẹn kiểm tra lại sau 7 ngày.")
            return "Đã ghi nhận khiếu nại, chuyển sang Chờ ĐVVC."

        if action == "compensation":
            amount = _amount(params.get("amount"), "Số tiền bồi thường")
            conn.execute("UPDATE cases SET compensated = compensated + ? WHERE id = ?", (amount, case["id"]))
            log(f"ĐVVC đã bồi thường {money(amount)}.")
            return f"Đã ghi nhận bồi thường {money(amount)}."

        raise ValueError("Thao tác không hợp lệ.")

    return _tx(conn, run)
