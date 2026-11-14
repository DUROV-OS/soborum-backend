from app.core.security import hash_password, verify_password


def test_admin_reset_allows_login_with_owner_requested_password(api, make_user):
    admin = make_user(admin=True)
    worker = make_user()
    response = api(admin).post(f"/api/auth/users/{worker.id}/reset-password")
    assert response.status_code == 200
    assert verify_password("12345678", worker.hashed_password)
    login = api(worker).post("/api/auth/login", data={"username": worker.email, "password": "12345678"})
    assert login.status_code == 200
    assert login.json()["access_token"]
    old_default = api(worker).post("/api/auth/login", data={"username": worker.email, "password": "password1234"})
    assert old_default.status_code == 401
    changed = api(worker).post("/api/auth/me/password", json={"current_password": "12345678", "new_password": "personal-password"})
    assert changed.status_code == 200
    assert api(worker).post("/api/auth/login", data={"username": worker.email, "password": "personal-password"}).status_code == 200
    assert api(worker).post("/api/auth/login", data={"username": worker.email, "password": "12345678"}).status_code == 401


def test_worker_cannot_reset_password(api, make_user):
    worker = make_user()
    other = make_user()
    response = api(worker).post(f"/api/auth/users/{other.id}/reset-password")
    assert response.status_code == 403


def test_reset_password_for_unknown_user_is_404(api, make_user):
    admin = make_user(admin=True)
    response = api(admin).post("/api/auth/users/999999/reset-password")
    assert response.status_code == 404


def test_user_can_change_own_password(api, make_user, db):
    user = make_user()
    user.hashed_password = hash_password("old-password")
    db.commit()
    response = api(user).post(
        "/api/auth/me/password",
        json={"current_password": "old-password", "new_password": "new-password"},
    )
    assert response.status_code == 200
    assert verify_password("new-password", user.hashed_password)


def test_change_password_rejects_wrong_current_password(api, make_user, db):
    user = make_user()
    user.hashed_password = hash_password("old-password")
    db.commit()
    response = api(user).post(
        "/api/auth/me/password",
        json={"current_password": "wrong", "new_password": "new-password"},
    )
    assert response.status_code == 400
    assert verify_password("old-password", user.hashed_password)


def test_change_password_rejects_short_new_password(api, make_user, db):
    user = make_user()
    user.hashed_password = hash_password("old-password")
    db.commit()
    response = api(user).post(
        "/api/auth/me/password",
        json={"current_password": "old-password", "new_password": "short"},
    )
    assert response.status_code == 422
