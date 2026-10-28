"""GĐ6.6 /api/v1/store chuẩn LangGraph, kiểm bằng chính langgraph_sdk.get_client().store (X-Api-Key). Server local :8020, embedding giả ($0).
Lưu ý: dự án này phục vụ API LangGraph dưới /api/v1 (như p5_sdk), nên url của client là http://127.0.0.1:8020/api/v1."""
import asyncio, uuid
import httpx, pytest
from langgraph_sdk import get_client
from test_fixes import B, ADMIN, a  # noqa: F401

URL = B + "/api/v1"
PW = "t66-Passw0rd-xyz"
run = asyncio.run

@pytest.fixture(scope="module")
def users(a):
    tag = uuid.uuid4().hex[:6]; made = []; out = {}
    for n in ("u1", "u2"):
        name = f"t66{n}{tag}"
        uid = a.post("/api/admin/users", json={"username": name, "password": PW, "role": "editor"}); assert uid.status_code == 200, uid.text
        with httpx.Client(base_url=B, auth=(name, PW), timeout=30) as c:
            k = c.post("/api/auth/keys", json={"name": "t66"}); assert k.status_code == 200, k.text
        out[n] = {"key": k.json()["key"], "id": uid.json()["id"], "name": name}; made.append(uid.json()["id"])
    ak = a.post("/api/auth/keys", json={"name": "t66-admin"}).json(); out["admin"] = {"key": ak["key"], "kid": ak["id"]}
    yield out
    a.delete(f"/api/auth/keys/{ak['id']}")
    for i in made: a.delete(f"/api/admin/users/{i}")

def sdk(u): return get_client(url=URL, api_key=u["key"]).store

def test_five_ops_with_sdk(users):
    s = sdk(users["u1"]); ns = ["t66", "notes"]
    async def go():
        await s.put_item(ns, key="a", value={"text": "Con mèo ngồi trên tấm thảm", "tag": "pets"})
        await s.put_item(ns, key="b", value={"text": "Báo cáo doanh thu quý 3 tăng mạnh", "tag": "biz"})
        it = await s.get_item(ns, key="a")
        assert it["namespace"] == ns and it["key"] == "a" and it["value"]["tag"] == "pets" and it["created_at"] and it["updated_at"]
        r = await s.search_items(["t66"], filter={"tag": "biz"})
        assert [x["key"] for x in r["items"]] == ["b"] and r["items"][0]["namespace"] == ns
        r = await s.search_items(["t66"], limit=1, offset=1); assert len(r["items"]) == 1
        nss = await s.list_namespaces(prefix=["t66"]); assert nss["namespaces"] == [ns]
        assert (await s.list_namespaces(prefix=["t66"], max_depth=1))["namespaces"] == [["t66"]]
        assert (await s.list_namespaces(suffix=["notes"]))["namespaces"] == [ns]
        await s.delete_item(ns, key="a")
        with pytest.raises(Exception): await s.get_item(ns, key="a")
        r = await s.search_items(["t66"]); assert [x["key"] for x in r["items"]] == ["b"]
        await s.delete_item(ns, key="b")
        assert (await s.search_items(["t66"]))["items"] == []
    run(go())

def test_semantic_search_ranks_relevant_first(users):
    s = sdk(users["u1"]); ns = ["t66", "kb"]
    docs = {"k1": "Cách đặt lại mật khẩu tài khoản", "k2": "Chính sách hoàn tiền trong 30 ngày", "k3": "Lịch bảo trì hệ thống hằng tuần"}
    async def go():
        for k, t in docs.items(): await s.put_item(ns, key=k, value={"text": t})
        try:
            r = await s.search_items(ns, query=docs["k2"], limit=3)    # embedding giả (băm) -> cùng văn bản = vector trùng nhau
            assert r["items"][0]["key"] == "k2" and r["items"][0].get("score") is not None and len(r["items"]) == 3
            r = await s.search_items(ns, query="bất kỳ", filter={"text": docs["k3"]}); assert [x["key"] for x in r["items"]] == ["k3"]
        finally:
            for k in docs: await s.delete_item(ns, key=k)
    run(go())

def test_two_users_cannot_see_each_other(users):
    s1, s2 = sdk(users["u1"]), sdk(users["u2"]); ns = ["t66", "private"]
    async def go():
        await s1.put_item(ns, key="secret", value={"text": "của user 1"})
        await s2.put_item(ns, key="secret", value={"text": "của user 2"})             # cùng namespace+key nhưng là mục khác
        try:
            assert (await s1.get_item(ns, key="secret"))["value"]["text"] == "của user 1"
            assert (await s2.get_item(ns, key="secret"))["value"]["text"] == "của user 2"
            assert [x["value"]["text"] for x in (await s2.search_items([]))["items"]] == ["của user 2"]
            # thử thoát tiền tố: nhãn "ws"/"user" do client gửi vẫn chỉ nằm SAU tiền tố của chính mình
            assert (await s2.search_items(["ws"]))["items"] == [] and (await s2.search_items(["user"]))["items"] == []
            assert (await s2.list_namespaces())["namespaces"] == [ns]
            await s2.delete_item(ns, key="secret")
            assert (await s1.get_item(ns, key="secret"))["value"]["text"] == "của user 1"      # xoá của u2 không đụng u1
        finally:
            await s1.delete_item(ns, key="secret"); await s2.delete_item(ns, key="secret")
    run(go())

def test_admin_sees_both_under_workspace(users):
    s1, s2, sa = sdk(users["u1"]), sdk(users["u2"]), sdk(users["admin"]); ns = ["t66", "shared"]
    async def go():
        await s1.put_item(ns, key="x", value={"text": "u1"}); await s2.put_item(ns, key="y", value={"text": "u2"})
        await sa.put_item(["t66", "wsnote"], key="z", value={"text": "admin"})
        try:
            seen = {(tuple(x["namespace"]), x["key"]) for x in (await sa.search_items(["user"]))["items"]}
            assert (("user", str(users["u1"]["id"]), "t66", "shared"), "x") in seen and (("user", str(users["u2"]["id"]), "t66", "shared"), "y") in seen
            assert (await sa.get_item(["user", str(users["u1"]["id"]), "t66", "shared"], key="x"))["value"]["text"] == "u1"
            nss = (await sa.list_namespaces())["namespaces"]
            assert ["user", str(users["u1"]["id"]), "t66", "shared"] in nss and ["t66", "wsnote"] in nss
            assert (await s1.get_item(["t66", "shared"], key="x"))["value"]["text"] == "u1"
            with pytest.raises(Exception): await s1.get_item(["t66", "wsnote"], key="z")      # mục chung của admin: user thường không thấy
        finally:
            await s1.delete_item(ns, key="x"); await s2.delete_item(ns, key="y"); await sa.delete_item(["t66", "wsnote"], key="z")
    run(go())

def test_validation_and_auth(users, a):
    def c(key=None): return httpx.Client(base_url=URL, headers={"X-Api-Key": key} if key else {}, timeout=30)
    with c() as anon:
        assert anon.get("/store/items", params={"namespace": "a", "key": "k"}).status_code == 401
        assert anon.post("/store/items/search", json={}).status_code == 401
    with c(users["u1"]["key"]) as u:
        assert u.put("/store/items", json={"namespace": [], "key": "k", "value": {}}).status_code == 422
        assert u.put("/store/items", json={"namespace": ["a", ""], "key": "k", "value": {}}).status_code == 422
        assert u.put("/store/items", json={"namespace": ["a.b"], "key": "k", "value": {}}).status_code == 422
        assert u.get("/store/items", params={"namespace": "nope", "key": "k"}).status_code == 404
    # mục cũ qua /api/store/* vẫn hoạt động
    r = a.post("/api/store/items", json={"ns": "t66old/x", "text": "legacy"}); assert r.status_code == 200
    assert any(i["key"] == r.json()["key"] for i in a.get("/api/store/items", params={"ns": "t66old/x"}).json())
    a.delete("/api/store/items", params={"ns": "t66old/x", "key": r.json()["key"]})
