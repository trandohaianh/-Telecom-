"""Fast Vietnamese accent folding + leetspeak normalisation (str.translate, no per-char Python loop)."""
import unicodedata

# _SRC là bộ ký tự có dấu tiếng Việt cần bỏ dấu để chuẩn hóa text đầu vào.
_SRC = ("àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ")
_DST = ("aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd")
_TABLE = str.maketrans(_SRC + _SRC.upper(), _DST + _DST)
# _LEET chuyển leetspeak / obfuscation thường gặp trong tin nhắn rác về dạng chuẩn.
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "@": "a", "$": "s"})


# normalize đưa text về chữ thường, bỏ dấu và giải mã biến thể để match với mẫu scam.
def normalize(s: str) -> str:
    s = s.lower().translate(_TABLE)
    if not s.isascii():
        s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    # undo common obfuscation inside words (v1etcombank -> vietcombank) but keep pure numbers
    return " ".join(w.translate(_LEET) if any(ch.isalpha() for ch in w) else w for w in s.split())
