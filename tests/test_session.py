from applypilot.session import classify_session_page


def test_applicant_profile_with_account_marker_confirms():
    result = classify_session_page("https://krasnoyarsk.hh.ru/applicant/profile/me", "Мои резюме Выйти")

    assert result.status == "confirmed"


def test_applicant_profile_without_account_marker_is_unknown():
    result = classify_session_page("https://krasnoyarsk.hh.ru/applicant/profile/me", "")

    assert result.status == "unknown"


def test_login_redirect_is_expired():
    result = classify_session_page("https://hh.ru/account/login", "")

    assert result.status == "expired"


def test_non_hh_applicant_url_is_not_trusted():
    result = classify_session_page("https://example.com/applicant/profile/me", "")

    assert result.status == "unknown"
