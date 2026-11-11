"""Environment presets and Vietnamese cultural vocabulary (S04).

The registry is data, not a model: every suggestion is derived from raw S03
observations with explicit keyword rules. No prompt is changed here and no
inference is triggered by switching a profile.

Guardrails encoded in the data:

* ``wedding_traditional`` never labels a person's role (bride, groom, mother...)
  without a user annotation; preparation labels only describe visible activity.
* an altar label needs ritual signals (shrine/altar/incense/offering/worship);
  a decorated table with flowers is a decoration or banquet table, not an altar.
* ``general`` has no wedding vocabulary at all, so it cannot invent a wedding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROFILE_VERSION = "s04-context-1.0.0"


@dataclass(frozen=True)
class LabelSpec:
    key: str
    label_vi: str
    category: str
    signals: tuple[str, ...]
    required_any: tuple[str, ...] = ()
    min_hits: int = 2
    description_vi: str = ""
    uncertainty_vi: str = ""
    #: Labels that must never be emitted as a confirmed identity.
    identity_label: bool = False


@dataclass(frozen=True)
class ProfileSpec:
    key: str
    label_vi: str
    description_vi: str
    labels: tuple[LabelSpec, ...]
    guardrails: tuple[str, ...] = ()
    default: bool = False


RITUAL_SIGNALS = (
    "shrine",
    "altar",
    "incense",
    "offering",
    "worship",
    "ancestor",
    "statue",
    "candle",
    "fruit tray",
    "thờ",
    "bàn thờ",
    "gia tiên",
    "hương",
    "lễ",
)

WEDDING_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec(
        "ancestor_altar",
        "Bàn gia tiên",
        "ritual",
        (
            "shrine",
            "altar",
            "incense",
            "offering",
            "worship",
            "ancestor",
            "statue",
            "candle",
            "fruit tray",
            "bàn thờ",
            "gia tiên",
            "thờ",
            "hương",
        ),
        required_any=("shrine", "altar", "incense", "worship", "ancestor", "bàn thờ", "gia tiên", "thờ"),
        min_hits=3,
        description_vi="Khu vực có bàn thờ/shrine, hương, lễ vật và tượng thờ.",
        uncertainty_vi="Chưa thể khẳng định đây là bàn gia tiên nếu thiếu hương/lễ vật/bối cảnh nghi lễ.",
    ),
    LabelSpec(
        "ancestor_backdrop",
        "Phông gia tiên / trang trí gia tiên",
        "ritual",
        ("backdrop", "banner", "red background", "flower arch", "names", "phông", "gia tiên", "chữ"),
        required_any=("backdrop", "banner", "flower arch", "phông"),
        min_hits=2,
        description_vi="Phông/banner trang trí phía sau khu vực lễ.",
        uncertainty_vi="Phông trang trí không đồng nghĩa với bàn thờ gia tiên.",
    ),
    LabelSpec(
        "wedding_gate",
        "Cổng cưới",
        "entrance",
        ("wedding gate", "arch", "entrance", "balloon", "welcome sign", "cổng cưới", "cổng"),
        required_any=("wedding gate", "cổng cưới", "arch"),
        min_hits=2,
        description_vi="Cổng/arch trang trí ở lối vào.",
        uncertainty_vi="Cổng hoa có thể dùng cho sự kiện khác, không chỉ đám cưới.",
    ),
    LabelSpec(
        "offering_trays",
        "Mâm quả / tráp lễ",
        "ritual",
        ("tray", "fruit", "betel", "areca", "lacquer", "red cloth", "mâm quả", "tráp", "trầu cau"),
        required_any=("tray", "mâm quả", "tráp", "betel", "areca"),
        min_hits=2,
        description_vi="Mâm/tráp đựng lễ vật, thường có trầu cau hoặc hoa quả.",
        uncertainty_vi="Mâm quả cũng có thể xuất hiện trong lễ khác.",
    ),
    LabelSpec(
        "bride_groom_prep",
        "Cô dâu, chú rể chuẩn bị",
        "people",
        ("dress", "suit", "makeup", "getting ready", "bouquet", "mirror", "váy", "vest", "trang điểm"),
        required_any=("dress", "suit", "makeup", "bouquet", "váy", "vest"),
        min_hits=2,
        description_vi="Người đang chuẩn bị trang phục/trang điểm trước lễ.",
        uncertainty_vi="Không xác định danh tính hoặc quan hệ của người trong frame.",
        identity_label=True,
    ),
    LabelSpec(
        "offering_procession",
        "Dàn bưng quả",
        "ritual",
        ("line of people", "carrying trays", "procession", "bưng quả", "đội hình"),
        required_any=("carrying trays", "bưng quả"),
        min_hits=2,
        description_vi="Nhóm người đi thành hàng, mang mâm/tráp.",
        uncertainty_vi="Chỉ là gợi ý về đội hình; không khẳng định vai trò từng người.",
        identity_label=True,
    ),
    LabelSpec(
        "ancestor_ceremony",
        "Lễ gia tiên",
        "ritual",
        ("ceremony", "bowing", "kneeling", "praying", "lễ gia tiên", "lạy", "quỳ"),
        required_any=("ceremony", "bowing", "kneeling", "praying", "lạy", "quỳ"),
        min_hits=2,
        description_vi="Hoạt động nghi lễ trước bàn thờ/gia tiên.",
        uncertainty_vi="Một tư thế trong frame chưa đủ khẳng định toàn bộ nghi lễ.",
    ),
    LabelSpec(
        "incense_offering",
        "Thắp hương / dâng lễ",
        "ritual",
        ("incense", "incense burner", "holding incense", "offering", "hương", "dâng lễ"),
        required_any=("incense", "holding incense", "hương"),
        min_hits=2,
        description_vi="Hương/đèn và người dâng lễ.",
        uncertainty_vi="Không suy ra lời khấn hay nội dung nghi lễ.",
    ),
    LabelSpec(
        "bride_procession",
        "Rước dâu / đón dâu",
        "ritual",
        ("car", "procession", "crowd", "gate", "rước dâu", "đón dâu", "đoàn"),
        required_any=("rước dâu", "đón dâu", "procession"),
        min_hits=2,
        description_vi="Đoàn người/xe di chuyển trong bối cảnh đón dâu.",
        uncertainty_vi="Không xác định danh tính cô dâu/chú rể nếu không có ghi chú.",
        identity_label=True,
    ),
    LabelSpec(
        "family_photos",
        "Chụp ảnh gia đình",
        "people",
        ("camera", "posing", "group photo", "photographer", "chụp ảnh", "chụp hình"),
        required_any=("camera", "photographer", "chụp ảnh", "chụp hình"),
        min_hits=2,
        description_vi="Người chụp ảnh và nhóm người đang tạo dáng.",
        uncertainty_vi="Không xác định quan hệ gia đình nếu người dùng chưa ghi chú.",
        identity_label=True,
    ),
    LabelSpec(
        "interview",
        "Phỏng vấn",
        "media",
        ("microphone", "interview", "reporter", "phỏng vấn", "mic"),
        required_any=("microphone", "interview", "phỏng vấn", "mic"),
        min_hits=2,
        description_vi="Micro/người phỏng vấn trong frame.",
        uncertainty_vi="Không suy ra nội dung lời nói từ hình ảnh.",
    ),
    LabelSpec(
        "wedding_reception",
        "Tiệc cưới",
        "banquet",
        ("long table", "banquet", "guests", "stage", "chairs", "plates", "tiệc", "bàn tiệc"),
        required_any=("long table", "banquet", "tiệc", "bàn tiệc"),
        min_hits=2,
        description_vi="Bàn tiệc dài/khách mời/sân khấu.",
        uncertainty_vi="Bàn tiệc không phải bàn thờ gia tiên.",
    ),
    LabelSpec(
        "wedding_decoration",
        "Bàn trang trí lễ cưới",
        "decoration",
        ("flowers", "floral", "centerpiece", "decorated table", "hoa", "trang trí"),
        required_any=("flowers", "floral", "centerpiece", "hoa", "trang trí"),
        min_hits=2,
        description_vi="Bàn được trang trí hoa/centerpiece, không có dấu hiệu thờ tự.",
        uncertainty_vi="Trang trí hoa một mình không đủ để gọi là bàn gia tiên.",
    ),
    LabelSpec(
        "banquet_table",
        "Bàn tiệc",
        "banquet",
        ("table", "plates", "food", "glasses", "chairs", "bàn", "đĩa", "thức ăn"),
        required_any=("table", "bàn"),
        min_hits=3,
        description_vi="Bàn có chén đĩa/thức ăn/ghế.",
        uncertainty_vi="Bàn có hoa và chén đĩa vẫn là bàn tiệc nếu thiếu dấu hiệu thờ tự.",
    ),
)

GENERAL_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec("indoor_room", "Không gian trong nhà", "scene", ("indoor", "room", "ceiling", "wall", "trong nhà", "phòng"), min_hits=2, description_vi="Không gian trong nhà."),
    LabelSpec("outdoor_scene", "Không gian ngoài trời", "scene", ("outdoor", "sky", "street", "tree", "ngoài trời", "đường"), min_hits=2, description_vi="Không gian ngoài trời."),
    LabelSpec("people_present", "Có người", "people", ("person", "people", "man", "woman", "người", "đàn ông", "phụ nữ"), min_hits=1, description_vi="Có người trong frame.", uncertainty_vi="Không xác định danh tính.", identity_label=True),
    LabelSpec("table_setting", "Bàn được bày biện", "object", ("table", "chairs", "plates", "bàn", "ghế"), min_hits=2, description_vi="Bàn/ghế được bày biện."),
    LabelSpec("text_visible", "Chữ hiển thị", "text", ("text", "sign", "banner", "chữ", "bảng"), min_hits=1, description_vi="Có chữ/bảng hiển thị."),
)

EVENT_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec("stage", "Sân khấu", "event", ("stage", "backdrop", "sân khấu", "phông"), min_hits=2, description_vi="Sân khấu/phông sự kiện."),
    LabelSpec("audience", "Khán giả", "event", ("audience", "crowd", "seats", "khán giả", "đám đông"), min_hits=2, description_vi="Khán giả/ghế ngồi."),
    LabelSpec("speaker", "Người phát biểu", "event", ("microphone", "speaker", "speech", "phát biểu", "mic"), min_hits=2, description_vi="Người cầm micro/phát biểu.", identity_label=True),
)

VLOG_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec("selfie_view", "Góc selfie/POV", "vlog", ("selfie", "camera held", "pov", "tự quay"), min_hits=1, description_vi="Góc máy cầm tay/selfie."),
    LabelSpec("street_food", "Đồ ăn đường phố", "vlog", ("street food", "food stall", "quán", "đồ ăn"), min_hits=2, description_vi="Quán/đồ ăn đường phố."),
    LabelSpec("landscape", "Cảnh quan", "vlog", ("mountain", "beach", "river", "landscape", "núi", "biển", "sông"), min_hits=2, description_vi="Cảnh quan thiên nhiên."),
)

DOCUMENTARY_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec("interview_setting", "Bối cảnh phỏng vấn", "documentary", ("interview", "microphone", "phỏng vấn", "mic"), min_hits=2, description_vi="Bối cảnh phỏng vấn."),
    LabelSpec("archive_object", "Vật chứng/tư liệu", "documentary", ("document", "photograph", "archive", "tài liệu", "ảnh cũ"), min_hits=2, description_vi="Tài liệu/vật chứng."),
)

POV_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec("first_person_motion", "Góc nhìn thứ nhất", "pov", ("pov", "walking", "hands", "first person", "góc nhìn"), min_hits=2, description_vi="Góc nhìn thứ nhất/đi bộ."),
    LabelSpec("route_scene", "Tuyến di chuyển", "pov", ("road", "path", "stairs", "đường", "cầu thang"), min_hits=2, description_vi="Tuyến đường/lối đi."),
)

PROFILES: dict[str, ProfileSpec] = {
    "general": ProfileSpec("general", "Tự động / Tổng quát", "Chỉ mô tả những gì nhìn thấy, không suy diễn nghi lễ.", GENERAL_LABELS, ("Không có từ vựng đám cưới.",), default=True),
    "wedding_traditional": ProfileSpec("wedding_traditional", "Đám cưới truyền thống Việt Nam", "Gợi ý nhãn nghi lễ/cưới truyền thống từ bằng chứng thị giác.", WEDDING_LABELS, ("Không gán vai trò người.", "Bàn trang trí không phải bàn gia tiên.", "Không suy ra lời khấn/âm thanh.")),
    "event": ProfileSpec("event", "Sự kiện", "Sân khấu, khán giả, phát biểu.", EVENT_LABELS),
    "vlog_travel": ProfileSpec("vlog_travel", "Đời sống / Du lịch / Vlog", "Góc máy, cảnh quan, đồ ăn.", VLOG_LABELS),
    "documentary": ProfileSpec("documentary", "Phóng sự / Tài liệu", "Phỏng vấn, tư liệu.", DOCUMENTARY_LABELS),
    "pov_footage": ProfileSpec("pov_footage", "POV", "Góc nhìn thứ nhất, tuyến di chuyển.", POV_LABELS),
    "custom": ProfileSpec("custom", "Tùy chỉnh", "Chưa có từ vựng tùy chỉnh; chỉ raw evidence.", ()),
}

PROFILE_IDS = tuple(PROFILES)
DEFAULT_PROFILE = "general"


def profile(profile_id: str) -> ProfileSpec:
    try:
        return PROFILES[profile_id]
    except KeyError as exc:
        raise KeyError(f"unknown analysis profile: {profile_id!r}") from exc


def profile_descriptors() -> list[dict[str, Any]]:
    return [
        {
            "key": spec.key,
            "label_vi": spec.label_vi,
            "description_vi": spec.description_vi,
            "guardrails": list(spec.guardrails),
            "label_count": len(spec.labels),
        }
        for spec in PROFILES.values()
    ]


def label(profile_id: str, label_key: str) -> LabelSpec | None:
    return next((item for item in profile(profile_id).labels if item.key == label_key), None)


def is_wedding_label(profile_id: str, label_key: str) -> bool:
    return profile_id == "wedding_traditional" and label(profile_id, label_key) is not None
