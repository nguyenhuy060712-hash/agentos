import pytest

def tra_loi(q):  # "ứng dụng" giả lập
    return {"Mấy giờ rồi?": "Bây giờ là 10:30", "Thủ đô Việt Nam?": "Hà Nội"}.get(q, "không biết")

@pytest.mark.agentos
@pytest.mark.parametrize("q,ref", [("Mấy giờ rồi?", "10:30"), ("Thủ đô Việt Nam?", "Hà Nội"), ("2+2?", "4")])
def test_tra_loi(agentos, q, ref):
    agentos.log_inputs({"question": q}); agentos.log_reference({"answer": ref})
    out = tra_loi(q); agentos.log_outputs({"answer": out})
    agentos.log_feedback("khop", ref in out)
    assert ref in out
