"""GAQL-lekérdezések (Google Ads lekérdezőnyelv) – egy helyen, hogy a próbák a hivatalos v25 leíró ellen ellenőrizhessék a mezőneveket."""


def quote(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def ids_in(ids):
    return "(" + ", ".join(str(int(i)) for i in ids) + ")"


def between(since, until):
    return f"segments.date BETWEEN '{since:%Y-%m-%d}' AND '{until:%Y-%m-%d}'"


OWNED_CAMPAIGNS = (
    "SELECT campaign.id, campaign.name, campaign.status, campaign.primary_status, campaign.resource_name, campaign.campaign_budget, "
    "campaign.labels, campaign_budget.amount_micros FROM campaign WHERE campaign.status != 'REMOVED'")


def campaign_daily(ids, since, until):
    return ("SELECT campaign.id, segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros FROM campaign "
            f"WHERE campaign.id IN {ids_in(ids)} AND {between(since, until)}")


def keywords(ids, since, until):
    return ("SELECT campaign.id, ad_group.id, ad_group.name, ad_group_criterion.resource_name, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
            "ad_group_criterion.keyword.match_type, ad_group_criterion.status, ad_group_criterion.quality_info.quality_score, "
            "metrics.impressions, metrics.clicks, metrics.cost_micros FROM keyword_view "
            f"WHERE campaign.id IN {ids_in(ids)} AND ad_group_criterion.negative = FALSE AND {between(since, until)}")


def keyword_status(ids):
    """A pozitív kulcsszavak állapota (metrikák és dátum nélkül, így a nulla megjelenésűek is megjönnek): a „hány aktív van a csoportban” és a brief-újraellenőrzés alapja."""
    return ("SELECT campaign.id, ad_group.id, ad_group.name, ad_group_criterion.resource_name, ad_group_criterion.criterion_id, "
            "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group_criterion.status FROM ad_group_criterion "
            f"WHERE campaign.id IN {ids_in(ids)} AND ad_group_criterion.negative = FALSE AND ad_group_criterion.status != 'REMOVED'")


def disapproved_details(ids):
    """Az elutasított hirdetések szabályzati okai (téma, típus, kifogásolt szövegek)."""
    return ("SELECT campaign.id, ad_group.id, ad_group_ad.ad.id, ad_group_ad.policy_summary.policy_topic_entries "
            f"FROM ad_group_ad WHERE campaign.id IN {ids_in(ids)} AND ad_group_ad.policy_summary.approval_status = 'DISAPPROVED' "
            "AND ad_group_ad.status != 'REMOVED'")


def negatives(ids):
    return ("SELECT campaign.id, ad_group.id, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
            "ad_group_criterion.keyword.match_type, ad_group_criterion.status FROM ad_group_criterion "
            f"WHERE campaign.id IN {ids_in(ids)} AND ad_group_criterion.negative = TRUE AND ad_group_criterion.status != 'REMOVED'")


def campaign_negatives(ids):
    return ("SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.keyword.text, campaign_criterion.keyword.match_type "
            f"FROM campaign_criterion WHERE campaign.id IN {ids_in(ids)} AND campaign_criterion.negative = TRUE "
            "AND campaign_criterion.type = 'KEYWORD'")


def search_terms(ids, since, until):
    return ("SELECT campaign.id, ad_group.id, search_term_view.search_term, search_term_view.status, "
            "metrics.impressions, metrics.clicks, metrics.cost_micros FROM search_term_view "
            f"WHERE campaign.id IN {ids_in(ids)} AND {between(since, until)}")


def ads(ids, since, until):
    return ("SELECT campaign.id, ad_group.id, ad_group_ad.resource_name, ad_group_ad.ad.id, ad_group_ad.status, ad_group_ad.policy_summary.approval_status, "
            "ad_group_ad.policy_summary.review_status, ad_group_ad.ad.added_by_google_ads, metrics.impressions, metrics.clicks, "
            f"metrics.cost_micros FROM ad_group_ad WHERE campaign.id IN {ids_in(ids)} AND ad_group_ad.status != 'REMOVED' AND {between(since, until)}")


def asset_labels(ids):
    return ("SELECT campaign.id, ad_group.id, ad_group_ad.ad.id, ad_group_ad_asset_view.field_type, ad_group_ad_asset_view.performance_label, "
            "asset.id, asset.text_asset.text FROM ad_group_ad_asset_view "
            f"WHERE campaign.id IN {ids_in(ids)} AND ad_group_ad_asset_view.field_type IN ('HEADLINE', 'DESCRIPTION')")


def change_events(since_iso, until_iso, limit=1000):
    return ("SELECT change_event.change_date_time, change_event.change_resource_type, change_event.change_resource_name, "
            "change_event.user_email, change_event.client_type, change_event.resource_change_operation, change_event.changed_fields, "
            "change_event.campaign "
            f"FROM change_event WHERE change_event.change_date_time >= '{since_iso}' AND change_event.change_date_time <= '{until_iso}' "
            f"ORDER BY change_event.change_date_time DESC LIMIT {int(limit)}")


def ad_content(ids):
    """A reszponzív keresési hirdetések tartalma (szövegcseréhez): címek, leírások, útvonal, végső URL."""
    return ("SELECT campaign.id, ad_group.id, ad_group_ad.resource_name, ad_group_ad.ad.id, ad_group_ad.status, ad_group_ad.ad.final_urls, "
            "ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions, "
            "ad_group_ad.ad.responsive_search_ad.path1, ad_group_ad.ad.responsive_search_ad.path2 FROM ad_group_ad "
            f"WHERE campaign.id IN {ids_in(ids)} AND ad_group_ad.status != 'REMOVED'")
