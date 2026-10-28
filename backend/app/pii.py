"""GĐ9.8 PII detection + masking engine (Presidio 2.2.364 analyzer + Vietnamese recognizers).

Detection: Presidio `AnalyzerEngine` with our own `PatternRecognizer` subclasses (Presidio has no Vietnamese recognizers):
  VN_CCCD (12 digits, province-code prefix validation), VN_CMND (9 digits, context-gated), VN_PHONE (mobile prefixes 03/05/07/08/09, +84, landline),
  VN_TAX_ID (10 / 10-3 digits with the real checksum: weights 31,29,23,19,17,13,7,5,3, check = 10 - sum mod 11 - verified on public company numbers),
  VN_BANK_ACCOUNT (context-gated: STK / số tài khoản / bank names), VN_LICENSE_PLATE (car + motorbike formats, province code validation),
  VN_PASSPORT, DATE_OF_BIRTH (context-gated) + EMAIL_ADDRESS / IP_ADDRESS / CREDIT_CARD (Luhn) and the opt-in rule-based PERSON / ADDRESS.
Context words are matched diacritic-insensitively ("số CCCD", "so can cuoc", "SĐT"...). Every match gets a score; context raises it, a negative context
(order / invoice / tracking numbers) suppresses it. No spaCy model is needed: the pipeline uses Presidio's NoOpNlpEngine (RSS about 105 MB after
the first call, nothing before: the import is lazy). `AOS_PII_NER=1` (or workspace setting `ner`) additionally loads spaCy `xx_ent_wiki_sm`
for PERSON / LOCATION (+300 MB RSS, measured; see docs/PDPL_GHI_CHU.md).
Masking: modes `mask` ([PHONE]), `hash` ([PHONE#a1b2c3d4], stable per workspace salt) and `pseudonym` (<PHONE_1>, reversible inside one request:
`Pseudonymizer.restore()` puts the real values back into the model answer). Official LangChain `PIIMiddleware` plug-in: `vn_pii_middleware()`.
Text is NFC-normalised before analysis (output of a masked text is NFC)."""
from __future__ import annotations
import asyncio, hashlib, hmac, os, re, threading, time, unicodedata
from collections import Counter
from dataclasses import dataclass

# entity -> (tag used in [TAG] / <TAG_n>, Vietnamese label, English label, on by default)
ENTITIES = {
    "VN_PHONE": ("PHONE", "Số điện thoại", "Phone number", True),
    "VN_CCCD": ("CCCD", "Căn cước công dân (12 số)", "Citizen ID (12 digits)", True),
    "VN_CMND": ("CMND", "Chứng minh nhân dân (9 số)", "Old ID card (9 digits)", True),
    "VN_TAX_ID": ("MST", "Mã số thuế", "Tax code", True),
    "VN_BANK_ACCOUNT": ("STK", "Số tài khoản ngân hàng", "Bank account", True),
    "VN_LICENSE_PLATE": ("BIENSO", "Biển số xe", "Licence plate", True),
    "VN_PASSPORT": ("PASSPORT", "Số hộ chiếu", "Passport number", True),
    "DATE_OF_BIRTH": ("NGAYSINH", "Ngày sinh", "Date of birth", True),
    "EMAIL_ADDRESS": ("EMAIL", "Email", "Email", True),
    "IP_ADDRESS": ("IP", "Địa chỉ IP", "IP address", True),
    "CREDIT_CARD": ("CARD", "Số thẻ tín dụng", "Credit card", True),
    "PERSON": ("TEN", "Họ tên người", "Person name", False),
    "ADDRESS": ("DIACHI", "Địa chỉ nhà", "Street address", False),
    "LOCATION": ("DIADIEM", "Địa danh (cần mô hình NER)", "Location (needs NER model)", False),
}
DEFAULT_ENTITIES = [k for k, v in ENTITIES.items() if v[3]]
TAG2ENT = {v[0]: k for k, v in ENTITIES.items()}
MODES = ("mask", "hash", "pseudonym")
MAX_CHARS = 60_000   # longer texts are analysed in chunks cut at a newline/space

# ------------------------------------------------------------------ helpers
def fold(s: str) -> str:
    """lower-case, no diacritics (đ -> d): context words match with or without Vietnamese marks."""
    s = unicodedata.normalize("NFD", s.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()

def _words_rx(words) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(fold(w)).replace(r"\ ", r"\s+") for w in words) + r")(?![a-z0-9])")

CCCD_PROVINCES = set("001 002 004 006 008 010 011 012 014 015 017 019 020 022 024 025 026 027 030 031 033 034 035 036 037 038 040 042 044 045 046 048 049 051 052 054 056 058 060 062 064 066 067 068 070 072 074 075 077 079 080 082 083 084 086 087 089 091 092 093 094 095 096".split())
PLATE_PROVINCES = set("11 12 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 43 47 48 49 50 51 52 53 54 55 56 57 58 59 60 61 62 63 64 65 66 67 68 69 70 71 72 73 74 75 76 77 78 79 80 81 82 83 84 85 86 88 89 90 92 93 94 95 97 98 99".split())
MOBILE_PREFIX = set("032 033 034 035 036 037 038 039 052 056 058 059 070 076 077 078 079 081 082 083 084 085 086 087 088 089 090 091 092 093 094 095 096 097 098 099".split())
TAX_W = (31, 29, 23, 19, 17, 13, 7, 5, 3)

def tax_checksum_ok(d10: str) -> bool:
    return len(d10) == 10 and d10.isdigit() and 10 - sum(int(a) * b for a, b in zip(d10, TAX_W)) % 11 == int(d10[9])

def luhn_ok(d: str) -> bool:
    s = 0
    for i, c in enumerate(reversed(d)):
        n = int(c) * (2 if i % 2 else 1); s += n - 9 if n > 9 else n
    return s % 10 == 0

# ------------------------------------------------------------------ recognizers (built lazily: importing presidio costs ~95 MB)
_PRES: dict = {}

def _presidio():
    if _PRES: return _PRES
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry, RecognizerResult
    from presidio_analyzer.context_aware_enhancers import ContextAwareEnhancer
    from presidio_analyzer.nlp_engine import NoOpNlpEngine
    _PRES.update(AnalyzerEngine=AnalyzerEngine, Pattern=Pattern, PatternRecognizer=PatternRecognizer, RecognizerRegistry=RecognizerRegistry,
                 RecognizerResult=RecognizerResult, ContextAwareEnhancer=ContextAwareEnhancer, NoOpNlpEngine=NoOpNlpEngine)
    return _PRES

_NUM_L = r"(?<![\w.,+\-/@])"        # a standalone number is not glued to other digits/letters, decimals, dates, prices or e-mails
_NUM_R = r"(?![\w@]|[.,\-/]\d)"

def _build_recognizers():
    P = _presidio(); PR, Pat, RR = P["PatternRecognizer"], P["Pattern"], P["RecognizerResult"]

    class VN(PR):
        """Regex candidate -> `score()` (validation + context). Scores: bare structure 0.5-0.8, with a context word 0.9+."""
        ENTITY = ""; RX: re.Pattern = re.compile("x"); CTX: re.Pattern | None = None; NEG: re.Pattern | None = None
        BEFORE = 40; AFTER = 0; GROUP = 0
        def __init__(self):
            super().__init__(supported_entity=self.ENTITY, patterns=[Pat(self.ENTITY.lower(), self.RX.pattern, 0.5)], supported_language="vi", name=type(self).__name__)
        def ctx(self, text, s, e) -> bool:
            if not self.CTX: return False
            w = fold(text[max(0, s - self.BEFORE):s]) + (" " + fold(text[e:e + self.AFTER]) if self.AFTER else "")
            return bool(self.CTX.search(w))
        def neg(self, text, s, e) -> bool:
            return bool(self.NEG and self.NEG.search(fold(text[max(0, s - 30):s])))
        def score(self, text, s, e, m) -> float: return 0.0
        def analyze(self, text, entities=None, nlp_artifacts=None, regex_flags=None):
            out = []
            for m in self.RX.finditer(text):
                s, e = m.start(self.GROUP), m.end(self.GROUP)
                sc = self.score(text, s, e, m)
                if sc > 0:
                    out.append(RR(self.ENTITY, s, e, sc, recognition_metadata={RR.RECOGNIZER_NAME_KEY: self.name, RR.RECOGNIZER_IDENTIFIER_KEY: self.id}))
            return out

    class Cccd(VN):
        ENTITY = "VN_CCCD"; RX = re.compile(_NUM_L + r"\d{12}" + _NUM_R)
        CTX = _words_rx(["cccd", "can cuoc", "căn cước công dân", "so can cuoc", "the can cuoc", "cmnd", "cmt", "chung minh", "dinh danh", "so dinh danh", "citizen id", "id card", "identity card", "cc cd"])
        NEG = _words_rx(["don hang", "ma don", "so don", "hoa don", "van don", "ma van", "tracking", "order", "ref", "ma giao dich", "giao dich", "ma tham chieu", "ma so don", "so hoa don", "so hop dong", "ma phieu", "ma yeu cau", "ticket"])
        BEFORE = 45
        def score(self, text, s, e, m):
            d = m.group(0); ok = d[:3] in CCCD_PROVINCES and d[3] in "0123" and int(d[4:6]) <= (time.localtime().tm_year % 100 if d[3] in "23" else 99)
            if self.ctx(text, s, e): return 0.95 if d[:3] in CCCD_PROVINCES else 0.8
            if self.neg(text, s, e): return 0
            return 0.6 if ok else 0

    class Cmnd(VN):
        ENTITY = "VN_CMND"; RX = re.compile(_NUM_L + r"\d{9}" + _NUM_R)
        CTX = _words_rx(["cmnd", "cmt", "chung minh nhan dan", "chung minh thu", "so chung minh", "chung minh", "so cmnd", "old id"])
        BEFORE = 35
        def score(self, text, s, e, m): return 0.92 if self.ctx(text, s, e) else 0

    class Phone(VN):
        ENTITY = "VN_PHONE"
        RX = re.compile(r"(?<![\w.,+\-/@])(?:\(?\+\s?84\)?|\(?84\)?|\(?0)[ .\-]?\(?\d{1,4}\)?(?:[ .\-]?\d){6,9}(?![\w@]|[.,\-/]\d)")
        CTX = _words_rx(["sdt", "so dien thoai", "dien thoai", "dt", "di dong", "lien he", "lien lac", "hotline", "zalo", "phone", "tel", "mobile", "call", "goi", "nhan tin", "sms", "contact", "so may", "so di dong", "whatsapp", "viber"])
        NEG = _words_rx(["stk", "so tai khoan", "tai khoan", "cccd", "can cuoc", "mst", "ma so thue", "cmnd", "don hang", "ma don", "hoa don", "van don", "tracking", "order"])
        BEFORE = 28; AFTER = 0
        def score(self, text, s, e, m):
            raw = m.group(0); digits = re.sub(r"\D", "", raw)
            if digits.startswith("84") and not raw.lstrip("(").startswith("0"): digits = "0" + digits[2:]
            if len(digits) == 10 and digits[:3] in MOBILE_PREFIX: base = 0.8
            elif len(digits) == 11 and digits[:2] == "02" and (digits[2] in "48" or digits[3] != "0" or digits[2] != "0"):
                base = 0.5 if re.search(r"[ .\-)]", raw) else 0.55   # landline (024/028 + 8 digits, 02xx + 7 digits)
            else: return 0
            if len(re.findall(r"[ .\-]", raw)) > 4: return 0
            c = self.ctx(text, s, e)
            if c: return 0.95 if base >= 0.8 else 0.9
            if self.neg(text, s, e): return 0
            return base

    class TaxId(VN):
        ENTITY = "VN_TAX_ID"; RX = re.compile(_NUM_L + r"\d{10}(?:\s?-\s?\d{3})?(?!\d)(?![\w@]|[.,]\d)|" + _NUM_L + r"\d{12}" + _NUM_R)
        CTX = _words_rx(["mst", "ma so thue", "so thue", "tax code", "tax id", "tax number", "tin", "ma so doanh nghiep", "mstcn", "ma so thue ca nhan"])
        NEG = _words_rx(["ma don", "so don", "don hang", "van don", "tracking", "giao dich", "so hoa don", "ma van", "order", "ticket", "ma tham chieu"])
        BEFORE = 30
        def score(self, text, s, e, m):
            raw = m.group(0); d = re.sub(r"\D", "", raw); c = self.ctx(text, s, e)
            if len(d) == 12: return 0.9 if c else 0
            ok = tax_checksum_ok(d[:10]); br = len(d) == 13
            if c: return (0.97 if ok else 0.75) + (0.0 if not br else 0.0)
            if ok and br: return 0.85
            return 0.55 if ok and d[0] == "0" and not self.neg(text, s, e) else 0

    class Bank(VN):
        ENTITY = "VN_BANK_ACCOUNT"
        RX = re.compile(_NUM_L + r"\d{8,19}(?!\d)(?![\w@]|[.,]\d)|" + _NUM_L + r"\d{3,4}(?: \d{3,5}){1,4}(?!\d)(?![\w@]|[.,]\d)")
        CTX = _words_rx(["stk", "so tai khoan", "tai khoan ngan hang", "tai khoan", "tk", "so tk", "account number", "account no", "acc no", "acc", "so tai khoan ngan hang", "chuyen khoan vao", "chuyen khoan", "ck vao", "bank account"])
        BANKS = _words_rx(["vietcombank", "vcb", "techcombank", "tcb", "mb bank", "mbbank", "mb", "bidv", "vietinbank", "agribank", "acb", "vpbank", "sacombank", "tpbank", "shb", "ocb", "msb", "hdbank", "vib", "scb", "eximbank", "seabank", "lpbank", "lienvietpostbank", "nam a bank", "bac a bank", "abbank", "pvcombank", "ngan hang", "bank"])
        MONEY = re.compile(r"\s?(?:vnd|vnđ|đ|₫|d\b|đồng|dong|usd|k\b|tr\b|nghìn|ngàn|triệu|tỷ)", re.I)
        BEFORE = 38
        def score(self, text, s, e, m):
            d = re.sub(r"\D", "", m.group(0))
            if not 8 <= len(d) <= 19 or self.MONEY.match(text, e): return 0
            if self.ctx(text, s, e): return 0.9
            w = fold(text[max(0, s - 25):s] + " " + text[e:e + 25])
            return 0.75 if self.BANKS.search(w) else 0

    class Plate(VN):
        ENTITY = "VN_LICENSE_PLATE"
        RX = re.compile(r"(?<![\w.,\-])(\d{2})[ \-]?(?:[A-Z]{1,2}Đ?\d?|MĐ\d?|[A-Z]\d|LD|NG|NN|QT|CD)(?:[ \-]?\d{3}[. ]?\d{2}|[ \-]\d{4}|-\d{5}|\d{5})(?![\w]|[.,]\d)")
        CTX = _words_rx(["bien so", "bien kiem soat", "bks", "xe", "o to", "xe may", "plate", "license plate", "licence plate", "xe bien", "mang bien"])
        BEFORE = 30
        def score(self, text, s, e, m):
            raw = m.group(0)
            if m.group(1) not in PLATE_PROVINCES: return 0
            strong = bool(re.search(r"[\-.]", raw)) and bool(re.search(r"[A-ZĐ]", raw))
            if self.ctx(text, s, e): return 0.95
            return 0.7 if strong else 0

    class Passport(VN):
        ENTITY = "VN_PASSPORT"; RX = re.compile(_NUM_L + r"[A-Z]\d{7,8}(?![\w@])")
        CTX = _words_rx(["ho chieu", "passport", "so ho chieu", "so passport"])
        BEFORE = 30
        def score(self, text, s, e, m): return 0.92 if self.ctx(text, s, e) else 0

    class Dob(VN):
        ENTITY = "DATE_OF_BIRTH"; RX = re.compile(r"(?<![\w/])(?:0?[1-9]|[12]\d|3[01])[/\-.](?:0?[1-9]|1[0-2])[/\-.](?:19|20)\d{2}(?![\w/]|[.\-]\d)")
        CTX = _words_rx(["ngay sinh", "sinh ngay", "sinh nhat", "dob", "date of birth", "born", "sinh", "nam sinh", "ns", "ngay thang nam sinh", "birthday"])
        BEFORE = 28
        def score(self, text, s, e, m): return 0.9 if self.ctx(text, s, e) else 0

    class Email(VN):
        ENTITY = "EMAIL_ADDRESS"; RX = re.compile(r"(?<![\w.+\-])[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
        def score(self, text, s, e, m): return 0.95

    class Ip(VN):
        ENTITY = "IP_ADDRESS"; RX = re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w]|\.\d)")
        CTX = _words_rx(["ip", "dia chi ip", "address", "server", "host"])
        NEG = _words_rx(["phien ban", "version", "ver", "build", "v", "release", "api"])
        def score(self, text, s, e, m): return 0.9 if self.ctx(text, s, e) else (0 if self.neg(text, s, e) else 0.6)

    class Card(VN):
        ENTITY = "CREDIT_CARD"; RX = re.compile(_NUM_L + r"\d{4}(?:[ \-]?\d{4}){2,3}(?:[ \-]?\d{1,3})?(?![\w@]|[.,\-/]\d)")
        CTX = _words_rx(["the", "so the", "card", "credit", "visa", "mastercard", "jcb", "thanh toan", "cvv", "the tin dung", "the ghi no", "debit"])
        BEFORE = 30
        def score(self, text, s, e, m):
            d = re.sub(r"\D", "", m.group(0))
            if not 13 <= len(d) <= 19 or not luhn_ok(d): return 0
            return 0.95 if self.ctx(text, s, e) or d[0] in "3456" and len(d) in (15, 16) else 0.7

    # --- names / addresses (rule-based, opt-in)
    FAM = ("Nguyễn Trần Lê Phạm Hoàng Huỳnh Phan Vũ Võ Đặng Bùi Đỗ Hồ Ngô Dương Lý Đinh Đoàn Lương Trịnh Tạ Mai Lâm Cao Tô Trương Hà Thái Châu Kiều Quách Phùng Chu Tăng Diệp Giang Hứa Lưu Nghiêm Thạch Triệu Vương Lại Khúc Tống Văn Quang Thân").split()
    PLACES = ["Hà Nội", "Hồ Chí Minh", "Đà Nẵng", "Hải Phòng", "Cần Thơ", "Sài Gòn", "Hà Nam", "Hà Tĩnh", "Hà Giang", "Huế", "Nha Trang", "Đà Lạt", "Vũng Tàu", "Bình Dương", "Đồng Nai", "Quảng Ninh", "Việt Nam", "Thái Nguyên", "Nghệ An", "Lâm Đồng", "Phú Quốc", "Cà Mau", "Hà Đông", "Cầu Giấy"]
    VU = "A-ZĐÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴ"; VL = "a-zđàáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ"
    CAP = f"[{VU}][{VL}]+"
    UP = f"[{VU}]{{2,}}"
    NAME_NEG = re.compile(r"(?<![a-z0-9])(?:duong|pho|ngo|phuong|quan|truong|thpt|thcs|benh vien|cong vien|cho|cau|nga tu|khu|tinh|huyen|cong ty|ngan hang|cua hang|nha hang|cafe|hotel|khach san|dai hoc|thu vien|bao tang|chua|den|lang|xa|thi tran|tap doan|thuong hieu|nhan hieu|hang|shop|tiem|spa|phong kham|nha sach)(?:\s+(?:co phan|tnhh|mtv|cp|so|chuyen|quoc te|thanh pho|gia dinh|dien tu|thuong mai|dich vu|san xuat|xay dung|dau tu|phat trien|giai phap|cong nghe|van tai|xuat nhap khau|du lich|giao duc|tu van))*\s*$")

    class Person(VN):
        ENTITY = "PERSON"
        RX = re.compile(rf"(?<![\wÀ-ỹ])(?:(?P<t>(?:[Aa]nh|[Cc]hị|[Cc]hi|[Ôô]ng|[Oo]ng|[Bb]à|[Cc]ô|[Cc]hú|[Bb]ác|[Ee]m|[Bb]ạn|Mr\.?|Mrs\.?|Ms\.?|Dr\.?|ThS\.?|TS\.?|BS\.?|GS\.?|PGS\.?|KS\.?)\s+)(?P<g>{CAP}(?:\s+{CAP}){{0,3}})|(?P<f>(?:{'|'.join(FAM)})(?:\s+{CAP}){{1,3}})|(?P<u>(?:{'|'.join(f.upper() for f in FAM)})(?:\s+{UP}){{1,3}}))(?![\wÀ-ỹ])")
        def _trim(self, name):
            toks = name.split()
            while len(toks) > 1:
                tail = " ".join(toks[-2:]) if len(toks) > 2 else None; one = toks[-1]
                if tail and any(tail.lower() == p.lower() for p in PLACES): toks = toks[:-2]; continue
                if one in PLACES: toks = toks[:-1]; continue
                break
            return toks
        def analyze(self, text, entities=None, nlp_artifacts=None, regex_flags=None):
            out = []
            for m in self.RX.finditer(text):
                g = "g" if m.group("g") else ("f" if m.group("f") else "u")
                s = m.start(g); toks = self._trim(m.group(g)) if g != "u" else m.group(g).split()
                if g == "g" and len(toks) == 1 and toks[0] in PLACES: continue
                if g == "f" and len(toks) < 2: continue
                if g == "g" and m.group("t").strip().lower() == "bà" and toks[0] == "Rịa": continue
                if g == "g":   # a capitalised kinship word ("Bà" in "Hai Bà Trưng") counts as a title only at the start of a sentence / clause
                    tt = m.group("t").strip()
                    if tt[0].isupper() and tt.lower() in ("anh", "chị", "chi", "ông", "ong", "bà", "cô", "chú", "bác", "em", "bạn") and not re.search(r"(?:^|[.!?:;\n\"'(\-–,])\s*$", text[:m.start("t")]): continue
                if g == "f" and toks[0] in ("Văn", "Quang", "Thân", "Lại", "Tống", "Mai", "Cao", "Hà", "Lâm", "Châu", "Giang", "Tô", "Thái", "Chu") and len(toks) < 3: continue
                if NAME_NEG.search(fold(text[max(0, s - 40):s])): continue
                e = s + len(" ".join(toks)) if g != "u" else m.end(g)
                sc = 0.88 if g == "g" else (0.85 if g == "u" else (0.8 if len(toks) >= 3 else 0.72))
                out.append(RR(self.ENTITY, s, e, sc, recognition_metadata={RR.RECOGNIZER_NAME_KEY: self.name, RR.RECOGNIZER_IDENTIFIER_KEY: self.id}))
            return out

    class Address(VN):
        ENTITY = "ADDRESS"
        _N = rf"(?:[{VU}]\w*|\d+[A-Za-z]?)"
        RX = re.compile(rf"(?<![\w/])(?:[Ss]ố\s+)?\d{{1,4}}[A-Za-z]?(?:[/\-]\d{{1,4}}[A-Za-z]?){{0,3}},?\s+(?:đường|phố|ngõ|ngách|hẻm|khu phố|tổ dân phố|thôn|xóm|ấp|tổ)\s+{_N}(?:\s+{_N}){{0,4}}"
                        rf"(?:\s+(?:đường|phố|ngõ|ngách|hẻm)\s+{_N}(?:\s+{_N}){{0,4}}){{0,2}}"
                        rf"(?:,?\s+(?:phường|xã|thị trấn|quận|huyện|thị xã|thành phố|tỉnh|P\.|Q\.|TP\.?|TX\.|H\.)\s*{_N}(?:\s+{_N}){{0,3}}){{0,4}}")
        def score(self, text, s, e, m): return 0.9

    recs = [Cccd(), Cmnd(), Phone(), TaxId(), Bank(), Plate(), Passport(), Dob(), Email(), Ip(), Card(), Person(), Address()]
    return recs

_LOCK = threading.Lock()
_ENGINES: dict = {}

def _no_ctx_enhancer():
    CAE = _presidio()["ContextAwareEnhancer"]
    class NoCtx(CAE):   # our recognizers do their own diacritic-insensitive context scoring
        def __init__(self): super().__init__(context_similarity_factor=0, min_score_with_context_similarity=0, context_prefix_count=0, context_suffix_count=0)
        def enhance_using_context(self, text, raw_results, nlp_artifacts, recognizers, context=None): return raw_results
    return NoCtx()

def ner_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("xx_ent_wiki_sm") is not None
    except Exception:  # noqa: BLE001
        return False

def engine(ner: bool = False):
    """AnalyzerEngine singleton (pattern-only, or + spaCy xx_ent_wiki_sm NER when `ner` and the model is installed)."""
    ner = bool(ner) and ner_available()
    with _LOCK:
        if ner in _ENGINES: return _ENGINES[ner]
        P = _presidio(); reg = P["RecognizerRegistry"](supported_languages=["vi"])
        for r in _build_recognizers(): reg.add_recognizer(r)
        if ner:
            import spacy
            from presidio_analyzer.nlp_engine import SpacyNlpEngine
            from presidio_analyzer.predefined_recognizers import SpacyRecognizer
            class E(SpacyNlpEngine):
                def __init__(self, nlp):
                    super().__init__(models=[{"lang_code": "vi", "model_name": "xx_ent_wiki_sm"}]); self.nlp = {"vi": nlp}
            eng = E(spacy.load("xx_ent_wiki_sm"))
            reg.add_recognizer(SpacyRecognizer(supported_language="vi", supported_entities=["PERSON", "LOCATION"], ner_strength=0.8))
        else:
            eng = P["NoOpNlpEngine"](models=[{"lang_code": "vi", "model_name": "noop"}])
        _ENGINES[ner] = P["AnalyzerEngine"](nlp_engine=eng, registry=reg, supported_languages=["vi"], context_aware_enhancer=_no_ctx_enhancer())
        return _ENGINES[ner]

# ------------------------------------------------------------------ analysis
@dataclass
class Finding:
    entity: str
    start: int
    end: int
    score: float
    @property
    def tag(self): return ENTITIES[self.entity][0]

def _chunks(text):
    if len(text) <= MAX_CHARS: yield 0, text; return
    i = 0
    while i < len(text):
        j = min(len(text), i + MAX_CHARS)
        if j < len(text):
            k = max(text.rfind("\n", i, j), text.rfind(" ", i, j))
            j = k + 1 if k > i else j
        yield i, text[i:j]; i = j

def analyze(text: str, entities=None, threshold: float = 0.5, ner: bool = False) -> list[Finding]:
    """Non-overlapping findings (highest score wins, then the longer span), sorted by position. `text` must already be NFC."""
    if not text or not isinstance(text, str): return []
    want = [e for e in (entities or DEFAULT_ENTITIES) if e in ENTITIES]
    if not want: return []
    eng = engine(ner or os.environ.get("AOS_PII_NER") == "1")
    raw: list[Finding] = []
    for off, part in _chunks(text):
        for r in eng.analyze(text=part, language="vi", entities=want, score_threshold=threshold):
            if r.entity_type in want: raw.append(Finding(r.entity_type, r.start + off, r.end + off, float(r.score)))
    raw.sort(key=lambda f: (-f.score, -(f.end - f.start), f.start))
    kept: list[Finding] = []
    for f in raw:
        if all(f.end <= k.start or f.start >= k.end for k in kept): kept.append(f)
    return sorted(kept, key=lambda f: f.start)

# ------------------------------------------------------------------ masking
def canon(entity: str, v: str) -> str:
    if entity == "EMAIL_ADDRESS": return v.strip().lower()
    if entity == "VN_PHONE":
        d = re.sub(r"\D", "", v); return "0" + d[2:] if d.startswith("84") else d
    if entity in ("VN_LICENSE_PLATE", "VN_PASSPORT"): return re.sub(r"[\s.\-]", "", v).upper()
    if entity in ("PERSON", "ADDRESS"): return " ".join(v.split()).lower()
    return re.sub(r"\D", "", v) or v

class Pseudonymizer:
    """Reversible pseudonyms for ONE request: the same real value always gets the same `<PHONE_1>` token; `restore()` puts the originals back."""
    def __init__(self): self.fwd: dict = {}; self.rev: dict = {}; self.n: Counter = Counter()
    def token(self, entity: str, value: str) -> str:
        k = (entity, canon(entity, value))
        if k not in self.fwd:
            tag = ENTITIES[entity][0]; self.n[tag] += 1; t = f"<{tag}_{self.n[tag]}>"; self.fwd[k] = t; self.rev[t] = value
        return self.fwd[k]
    def __bool__(self): return bool(self.rev)
    _RX = re.compile(r"<?\b(" + "|".join(sorted({v[0] for v in ENTITIES.values()}, key=len, reverse=True)) + r")_(\d{1,4})\b>?")
    def restore(self, text):
        if not isinstance(text, str) or not self.rev: return text
        return self._RX.sub(lambda m: self.rev.get(f"<{m.group(1)}_{m.group(2)}>", m.group(0)), text)
    def restore_obj(self, o):
        if isinstance(o, str): return self.restore(o)
        if isinstance(o, list): return [self.restore_obj(x) for x in o]
        if isinstance(o, dict): return {k: self.restore_obj(v) for k, v in o.items()}
        return o

def normalize_cfg(c: dict | None) -> dict | None:
    """DB row / API body -> engine config. None when masking is off."""
    if not c or not c.get("enabled"): return None
    ents = [e for e in (c.get("entities") or DEFAULT_ENTITIES) if e in ENTITIES]
    return {"enabled": True, "mode": c.get("mode") if c.get("mode") in MODES else "mask", "entities": ents or DEFAULT_ENTITIES, "threshold": float(c.get("threshold") or 0.5),
            "traces": c.get("apply_traces", c.get("traces", True)) is not False, "gateway": c.get("apply_gateway", c.get("gateway", True)) is not False,
            "ner": bool(c.get("ner")), "salt": c.get("salt") or ""}

def _hash(salt: str, entity: str, v: str) -> str:
    return hmac.new((salt or "aos").encode(), f"{entity}:{canon(entity, v)}".encode(), hashlib.sha256).hexdigest()[:8]

def mask_text(text, cfg: dict | None = None, pz: Pseudonymizer | None = None, with_findings: bool = False):
    """Replace every finding in `text`. cfg: mode, entities, threshold, ner, salt (defaults: mask mode, DEFAULT_ENTITIES)."""
    cfg = cfg or {}
    if not isinstance(text, str) or not text: return (text, []) if with_findings else text
    t = unicodedata.normalize("NFC", text)
    fs = analyze(t, cfg.get("entities"), float(cfg.get("threshold") or 0.5), bool(cfg.get("ner")))
    if not fs: return (text, []) if with_findings else text
    mode = cfg.get("mode") if cfg.get("mode") in MODES else "mask"
    if mode == "pseudonym" and pz is None: pz = Pseudonymizer()
    out, last = [], 0
    for f in fs:
        v = t[f.start:f.end]; out.append(t[last:f.start])
        out.append(f"[{f.tag}]" if mode == "mask" else f"[{f.tag}#{_hash(cfg.get('salt', ''), f.entity, v)}]" if mode == "hash" else pz.token(f.entity, v))
        last = f.end
    out.append(t[last:])
    res = "".join(out)
    return (res, fs) if with_findings else res

def mask_obj(o, cfg=None, pz=None, skip_keys=()):
    """Mask every string leaf of a JSON-like structure (keys untouched)."""
    if isinstance(o, str): return mask_text(o, cfg, pz)
    if isinstance(o, list): return [mask_obj(x, cfg, pz, skip_keys) for x in o]
    if isinstance(o, dict): return {k: (v if k in skip_keys else mask_obj(v, cfg, pz, skip_keys)) for k, v in o.items()}
    return o

def scan_text(text, entities=None, threshold=0.5, ner=False):
    """[(kind-tag, start, end)] for the old gateway guardrail (block mode) and the test endpoint."""
    t = unicodedata.normalize("NFC", text or "")
    return [(f.tag, f.start, f.end) for f in analyze(t, entities, threshold, ner)]

# ------------------------------------------------------------------ LangChain v1 middleware (official PIIMiddleware + our detectors)
def detector(entity: str, threshold: float = 0.5, ner: bool = False):
    """`Callable[[str], list[PIIMatch]]` accepted by langchain.agents.middleware.PIIMiddleware(detector=...)."""
    def detect(content: str):
        t = unicodedata.normalize("NFC", content or "")
        return [{"type": entity.lower(), "value": t[f.start:f.end], "start": f.start, "end": f.end} for f in analyze(t, [entity], threshold, ner)]
    return detect

def vn_pii_middleware(entities=None, strategy: str = "redact", apply_to_input: bool = True, apply_to_output: bool = False, apply_to_tool_results: bool = False, threshold: float = 0.5):
    """One official `PIIMiddleware` per entity (state-level, streaming-safe; strategy block|redact|mask|hash). Use for agents that must never STORE PII.
    The reversible per-request pseudonymisation lives in the gateway (GatewayMiddleware) instead, so conversation state keeps the real values."""
    from langchain.agents.middleware import PIIMiddleware
    return [PIIMiddleware(e.lower(), strategy=strategy, detector=detector(e, threshold), apply_to_input=apply_to_input, apply_to_output=apply_to_output,
                          apply_to_tool_results=apply_to_tool_results) for e in (entities or DEFAULT_ENTITIES)]

# ------------------------------------------------------------------ per-workspace settings cache (DB reads in privacy.py)
_CFG: dict = {}
CFG_TTL = 20.0

def cache_put(ws, cfg): _CFG[ws] = (time.monotonic(), cfg)
def cache_get(ws):
    x = _CFG.get(ws)
    return (True, x[1]) if x and time.monotonic() - x[0] < CFG_TTL else (False, None)
def cache_clear(ws=None):
    if ws is None: _CFG.clear()
    else: _CFG.pop(ws, None)

async def _sys_row(sql, args):
    import gateway as gw
    return await gw._sys(sql, args, one=True)   # system query (RLS off); explicit workspace filter in every statement

async def workspace_cfg(ws):
    """Engine config of a workspace (None = masking off). Cached 20 s; settings PUT clears the cache.
    A lookup failure keeps the last known config (privacy settings must not silently switch off because of a DB hiccup)."""
    if not ws: return None
    hit, c = cache_get(ws)
    if hit: return c
    try:
        row = await _sys_row("SELECT * FROM pii_settings WHERE workspace_id=%s", (int(ws),))
    except Exception:  # noqa: BLE001   # no pool / table not created yet
        old = _CFG.get(ws); return old[1] if old else None
    c = normalize_cfg(row); cache_put(ws, c); return c

_PWS: dict = {}
async def project_cfg(pid):
    """Config for traces of a project (via the project's workspace). Demo workspaces never mask (feature off there)."""
    ws = _PWS.get(pid)
    if ws is None:
        try: r = await _sys_row("SELECT workspace_id FROM projects WHERE id=%s", (pid,))
        except Exception: return None  # noqa: BLE001
        if not r: return None
        ws = _PWS[pid] = r["workspace_id"]
    c = await workspace_cfg(ws)
    return c if c and c["traces"] else None

async def amask_obj(o, cfg, pz=None):
    """mask_obj off the event loop (regex work on big traces must not block other requests)."""
    return await asyncio.to_thread(mask_obj, o, cfg, pz)

# ------------------------------------------------------------------ gateway: mask the request, restore the answer
def _mask_content(content, cfg, pz):
    if isinstance(content, str): return mask_text(content, cfg, pz)
    if isinstance(content, list):
        return [({**b, "text": mask_text(b["text"], cfg, pz)} if isinstance(b, dict) and isinstance(b.get("text"), str) else mask_text(b, cfg, pz) if isinstance(b, str) else b) for b in content]
    return content

def _mask_message(m, cfg, pz):
    upd = {"content": _mask_content(m.content, cfg, pz)}
    if getattr(m, "tool_calls", None): upd["tool_calls"] = [{**tc, "args": mask_obj(tc.get("args"), cfg, pz)} for tc in m.tool_calls]
    return m.model_copy(update=upd)

def mask_request_sync(request, cfg):
    """(ModelRequest with masked messages + system prompt, Pseudonymizer or None). One Pseudonymizer per model call: tokens are stable inside the call."""
    pz = Pseudonymizer() if cfg["mode"] == "pseudonym" else None
    kw = {"messages": [_mask_message(m, cfg, pz) for m in request.messages]}
    sm = getattr(request, "system_message", None)
    if sm is not None: kw["system_message"] = _mask_message(sm, cfg, pz)
    return request.override(**kw), pz

async def mask_request(request, cfg):
    return await asyncio.to_thread(mask_request_sync, request, cfg)

def restore_response(resp, pz):
    """Put the real values back into the model answer (text + tool-call arguments) so the user, the state and the tools see real data."""
    if not pz: return resp
    for m in (getattr(resp, "result", None) or [resp]):
        if getattr(m, "type", "") != "ai": continue
        m.content = pz.restore_obj(m.content) if not isinstance(m.content, str) else pz.restore(m.content)
        if getattr(m, "tool_calls", None):
            for tc in m.tool_calls: tc["args"] = pz.restore_obj(tc.get("args"))
    return resp

# ------------------------------------------------------------------ GĐ9.P2: pseudonym restore while the answer is STREAMING (no `<PHONE_1>` flash)
_TAGS = sorted({v[0] for v in ENTITIES.values()}, key=len, reverse=True)
_HOLD_RX = re.compile(r"(?:<[A-Z_]*\d{0,4}|(?<![A-Za-z0-9_])[A-Z][A-Z_]*\d{0,4})$")   # a tail that may still grow into `<TAG_n>` / `TAG_n`

def _tag_prefix(s: str) -> bool:
    """Could `s` (without the leading '<') become `TAG_<digits>`? ('' counts: a lone '<')"""
    return s == "" or any(t.startswith(s) or (s.startswith(t + "_") and (s[len(t) + 1:].isdigit() or s == t + "_")) for t in _TAGS)

class StreamRestorer:
    """Text-stream version of `Pseudonymizer.restore`: `feed(chunk)` returns the text that is safe to show now (real values restored); an unfinished
    token at the end of the chunk ('<', '<PHO', 'PHONE_', '<PHONE_1') is held back until the next chunk completes it (tokens split across chunks work);
    `flush()` releases whatever is left at the end of the stream. Ordinary text is never delayed except a trailing `<` or a capital-letter word that
    is a prefix of a tag."""
    def __init__(self, pz): self.pz, self.buf = pz, ""
    def feed(self, text: str) -> str:
        if not text: return ""
        s = self.buf + text; m = _HOLD_RX.search(s)
        cut = m.start() if m and _tag_prefix(m.group(0).lstrip("<")) else len(s)
        self.buf = s[cut:]
        return self.pz.restore(s[:cut])
    def flush(self) -> str:
        s, self.buf = self.buf, ""
        return self.pz.restore(s)

class ChunkRestorer:
    """Applies StreamRestorer to the `content` of chat-model stream chunks (AIMessageChunk). Tool-call argument chunks are left alone here (the complete
    arguments are restored by `restore_response` before any tool runs)."""
    def __init__(self, pz): self.pz, self.text, self.done = pz, StreamRestorer(pz), False
    def _chunk(self, ch, content):
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk
        return ChatGenerationChunk(message=ch.message.model_copy(update={"content": content}), generation_info=ch.generation_info) if isinstance(ch.message, AIMessageChunk) else ch
    def feed(self, ch):
        m = ch.message; c = getattr(m, "content", None)
        if isinstance(c, str):
            s = self.text.feed(c)
            if getattr(m, "chunk_position", None) == "last": s += self.text.flush(); self.done = True
            return self._chunk(ch, s) if s != c else ch
        if isinstance(c, list):   # content blocks (v1 output): restore the text of each block on its own (tokens split across blocks are not joined)
            return self._chunk(ch, [({**b, "text": self.pz.restore(b["text"])} if isinstance(b, dict) and isinstance(b.get("text"), str) else b) for b in c])
        return ch
    def tail(self):
        s = self.text.flush()
        if not s: return None
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk
        return ChatGenerationChunk(message=AIMessageChunk(content=s))

async def arestore_chunks(agen, pz):
    r = ChunkRestorer(pz)
    async for ch in agen: yield r.feed(ch)
    t = r.tail()
    if t is not None: yield t

def restore_chunks(gen, pz):
    r = ChunkRestorer(pz)
    for ch in gen: yield r.feed(ch)
    t = r.tail()
    if t is not None: yield t
