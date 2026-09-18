"""Community forums: Reddit-style categories, posts, comments and likes.

- Healing and Creator members can browse, post, and comment.
- Free accounts (and guests) see a membership gate — no read, post, or reply.
- A profanity guard blocks unkind language, warns, then removes posting access.
- Likes are one-per-member toggles, mirroring the quote "heart" pattern.
"""
import logging

from flask import (abort, flash, redirect, render_template, request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload, selectinload

from ..extensions import db, limiter
from ..models import (LOOKING_FOR, LOOKING_FOR_SLUGS, ForumCategory, ForumComment,
                      ForumCommentLike, ForumImage, ForumPost, ForumPostLike,
                      ForumTag)
from ..services.moderation import contains_profanity, register_violation
from . import bp

log = logging.getLogger(__name__)

BANNED_NOTICE = ("Posting is paused for your account after repeated unkind "
                 "language. You can still read the community.")
MEMBER_NOTICE = ("Community is a members' circle — Healing, Creator, or Full Bloom "
                 "unlocks the rooms where we talk, listen, and bloom together.")


@bp.before_request
def _require_community_member():
    """Healing / Creator / Full Bloom (or owner) only — free and guests see the gate."""
    if getattr(current_user, "is_authenticated", False) and current_user.is_member():
        return None
    if request.method != "GET":
        if not getattr(current_user, "is_authenticated", False):
            flash("Sign in, then choose a membership to join the community.", "info")
            return redirect(url_for("auth.login", next=url_for("forums.index")))
        flash(MEMBER_NOTICE, "info")
        return redirect(url_for("main.membership", next=url_for("forums.index")))
    return render_template(
        "forums/gate.html",
        signed_in=bool(getattr(current_user, "is_authenticated", False)),
    )


def _can_access_category(cat) -> bool:
    """Healing rooms ↔ community_healing; Building rooms ↔ community_building."""
    if not getattr(current_user, "is_authenticated", False) or not current_user.is_member():
        return False
    slug = (getattr(cat, "slug", None) or "").lower()
    if slug == "building":
        return current_user.has_feature("community_building")
    if slug == "healing":
        return current_user.has_feature("community_healing")
    return True


def _require_category_access(cat):
    if _can_access_category(cat):
        return None
    if (getattr(cat, "slug", "") or "").lower() == "building":
        flash("The Building community isn’t included in your plan.", "info")
    else:
        flash("The Healing community isn’t included in your plan.", "info")
    return redirect(url_for("main.membership", next=request.path))


def _can_participate() -> bool:
    """True if the current user may like / interact in community."""
    return bool(getattr(current_user, "is_authenticated", False)
                and current_user.is_member()
                and not getattr(current_user, "forum_banned", False))


def _require_participant(redirect_to):
    """Flash + redirect if the current user can't interact; else None."""
    if not getattr(current_user, "is_authenticated", False):
        flash("Sign in to join the conversation.", "info")
        return redirect(url_for("auth.login", next=request.path))
    if not current_user.is_member():
        flash(MEMBER_NOTICE, "info")
        return redirect(url_for("main.membership", next=request.path))
    if current_user.forum_banned:
        flash(BANNED_NOTICE, "error")
        return redirect(redirect_to)
    return None


def _anon_default():
    return bool(getattr(current_user, "default_anonymous", False))


def _wants_anonymous():
    return request.form.get("anonymous") == "1"


def _guard_content(*texts) -> bool:
    """Returns True if the content is clean; otherwise warns/bans and flashes."""
    if any(contains_profanity(t) for t in texts if t):
        status = register_violation(current_user)
        flash(status["message"], "error")
        return False
    return True


@bp.route("/")
def index():
    cats = ForumCategory.query.order_by(ForumCategory.sort_order, ForumCategory.name).all()
    return render_template(
        "forums/index.html",
        cats_by_slug={c.slug: c for c in cats},
    )


@bp.route("/c/<slug>")
def category(slug):
    cat = ForumCategory.query.filter_by(slug=slug).first_or_404()
    blocked = _require_category_access(cat)
    if blocked:
        return blocked
    tags = cat.tags.all()
    active_tag = None
    tag_slug = request.args.get("tag")
    if tag_slug:
        active_tag = next((t for t in tags if t.slug == tag_slug), None)

    looking = (request.args.get("looking") or "").strip().lower()
    if looking not in LOOKING_FOR_SLUGS:
        looking = None

    query = (cat.posts.filter_by(hidden=False)
             .options(joinedload(ForumPost.author), joinedload(ForumPost.tag)))
    if active_tag:
        query = query.filter_by(tag_id=active_tag.id)
    if looking:
        query = query.filter_by(looking_for=looking)
    query = query.order_by(ForumPost.created_at.desc())

    can_participate = _can_participate()
    posts = query.limit(100).all()
    post_ids = [p.id for p in posts]
    comment_counts = {}
    like_counts = {}
    if post_ids:
        comment_counts = dict(
            db.session.query(ForumComment.post_id, func.count(ForumComment.id))
            .filter(
                ForumComment.post_id.in_(post_ids),
                ForumComment.hidden.is_(False),
            )
            .group_by(ForumComment.post_id)
            .all()
        )
        like_counts = dict(
            db.session.query(ForumPostLike.post_id, func.count(ForumPostLike.id))
            .filter(ForumPostLike.post_id.in_(post_ids))
            .group_by(ForumPostLike.post_id)
            .all()
        )

    from ..services.forum_quotas import can_free_post
    can_post, post_quota_msg = (True, "")
    if can_participate:
        can_post, post_quota_msg = can_free_post(current_user)

    def filter_url(**extra):
        args = {}
        tag_val = extra["tag"] if "tag" in extra else (active_tag.slug if active_tag else None)
        look_val = extra["looking"] if "looking" in extra else looking
        if tag_val:
            args["tag"] = tag_val
        if look_val:
            args["looking"] = look_val
        return url_for("forums.category", slug=cat.slug, **args)

    return render_template(
        "forums/category.html", category=cat, posts=posts,
        tags=tags, active_tag=active_tag,
        looking_for_options=LOOKING_FOR, active_looking=looking,
        anon_default=_anon_default(), view="list",
        can_participate=can_participate,
        can_post=can_post, post_quota_msg=post_quota_msg,
        filter_url=filter_url,
        comment_counts=comment_counts,
        like_counts=like_counts,
    )


@bp.route("/c/<slug>/new", methods=["POST"])
@login_required
@limiter.limit("15 per hour")
def create_post(slug):
    cat = ForumCategory.query.filter_by(slug=slug).first_or_404()
    blocked = _require_category_access(cat)
    if blocked:
        return blocked
    blocked = _require_participant(url_for("forums.category", slug=slug))
    if blocked:
        return blocked

    from ..services.forum_quotas import can_free_post
    ok, quota_msg = can_free_post(current_user)
    if not ok:
        flash(quota_msg, "info")
        from ..services.social_graph import notify
        notify(current_user.id, kind="quota", body=quota_msg,
               url=url_for("forums.category", slug=slug))
        db.session.commit()
        return redirect(url_for("forums.category", slug=slug))

    title = (request.form.get("title") or "").strip()[:160]
    body = (request.form.get("body") or "").strip()[:8000]
    if not title or not body:
        flash("A post needs a title and a few words.", "error")
        return redirect(url_for("forums.category", slug=slug))

    tag_id = None
    raw_tag = request.form.get("tag_id")
    if raw_tag and raw_tag.isdigit():
        tag = db.session.get(ForumTag, int(raw_tag))
        if tag and tag.category_id == cat.id:
            tag_id = tag.id

    looking = (request.form.get("looking_for") or "").strip().lower()
    if looking not in LOOKING_FOR_SLUGS:
        looking = None

    if not _guard_content(title, body):
        return redirect(url_for("forums.category", slug=slug))

    post = ForumPost(category_id=cat.id, tag_id=tag_id, looking_for=looking,
                     user_id=current_user.id,
                     title=title, body=body, anonymous=_wants_anonymous())
    db.session.add(post)
    db.session.flush()
    from ..services.community_images import attach_images
    _img_saved, _img_skipped = attach_images(
        request.files.getlist("images"), user=current_user, post=post)
    from ..services.social_graph import notify_followers_of_post, notify_mentions
    if not post.anonymous:
        notify_followers_of_post(current_user, post)
        notify_mentions(current_user, f"{title}\n{body}", post_id=post.id)
    db.session.commit()
    flash("Posted. Thank you for adding your voice.", "success")
    if _img_skipped:
        flash("Some images couldn't be added (max 4, images under 8 MB each).",
              "info")
    still_ok, exhausted_msg = can_free_post(current_user)
    if not still_ok:
        flash(exhausted_msg, "info")
        from ..services.social_graph import notify
        notify(current_user.id, kind="quota", body=exhausted_msg,
               url=url_for("forums.category", slug=slug))
        db.session.commit()
    return redirect(url_for("forums.post", post_id=post.id))


@bp.route("/p/<int:post_id>")
def post(post_id):
    post = db.session.get(ForumPost, post_id)
    if post is None or post.hidden:
        abort(404)
    blocked = _require_category_access(post.category)
    if blocked:
        return blocked
    can_participate = _can_participate()
    # top-level comments, each with its (one-level) replies
    top = (
        post.comments.filter_by(hidden=False, parent_id=None)
        .options(
            joinedload(ForumComment.author),
            selectinload(ForumComment.replies).joinedload(ForumComment.author),
        )
        .order_by(ForumComment.created_at)
        .all()
    )
    all_ids = []
    threads = []
    for c in top:
        replies = [r for r in c.replies if not r.hidden]
        threads.append((c, replies))
        all_ids.append(c.id)
        all_ids.extend(r.id for r in replies)
    liked_posts, liked_comments = _liked_ids([post_id], all_ids)

    from ..services.forum_quotas import can_free_reply
    can_reply, reply_quota_msg = (True, "")
    if can_participate:
        can_reply, reply_quota_msg = can_free_reply(current_user)

    return render_template("forums/post.html", post=post, threads=threads,
                           comment_count=len(all_ids),
                           liked_posts=liked_posts, liked_comments=liked_comments,
                           anon_default=_anon_default(),
                           can_participate=can_participate,
                           can_reply=can_reply, reply_quota_msg=reply_quota_msg)


def _can_reply_under_comment(post, comment) -> bool:
    """Top-level comments are open to anyone who can participate.

    Nested replies are limited: the post author (OP) may reply to any comment,
    and a comment's author may reply under their own comment.
    """
    if not getattr(current_user, "is_authenticated", False):
        return False
    if getattr(current_user, "forum_banned", False):
        return False
    if current_user.id == post.user_id:
        return True
    if current_user.id == comment.user_id:
        return True
    return False


@bp.route("/p/<int:post_id>/comment", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def create_comment(post_id):
    post = db.session.get(ForumPost, post_id)
    if post is None or post.hidden:
        abort(404)
    blocked = _require_participant(url_for("forums.post", post_id=post_id))
    if blocked:
        return blocked

    from ..services.forum_quotas import can_free_reply
    ok, quota_msg = can_free_reply(current_user)
    if not ok:
        flash(quota_msg, "info")
        from ..services.social_graph import notify
        notify(current_user.id, kind="quota", body=quota_msg,
               url=url_for("forums.post", post_id=post_id))
        db.session.commit()
        return redirect(url_for("forums.post", post_id=post_id))

    body = (request.form.get("body") or "").strip()[:4000]
    image_files = request.files.getlist("images")
    has_images = any(f and (getattr(f, "filename", "") or "").strip()
                     for f in image_files)
    if not body and not has_images:
        flash("Write a little something first.", "error")
        return redirect(url_for("forums.post", post_id=post_id))

    # optional reply target — flattened so threads never nest deeper than one level
    parent_id = None
    raw_parent = request.form.get("parent_id")
    if raw_parent and raw_parent.isdigit():
        parent = db.session.get(ForumComment, int(raw_parent))
        if parent is None or parent.post_id != post.id or parent.hidden:
            flash("That comment isn't available to reply to.", "error")
            return redirect(url_for("forums.post", post_id=post_id))
        # Always attach under the top-level comment in the thread.
        top = parent if parent.parent_id is None else db.session.get(ForumComment, parent.parent_id)
        if top is None or top.hidden:
            flash("That comment isn't available to reply to.", "error")
            return redirect(url_for("forums.post", post_id=post_id))
        if not _can_reply_under_comment(post, top):
            flash("Only the original poster can reply to others' comments. "
                  "You can still leave a top-level comment, or reply under your own.", "info")
            return redirect(url_for("forums.post", post_id=post_id) + "#comments")
        parent_id = top.id

    if not _guard_content(body):
        return redirect(url_for("forums.post", post_id=post_id))

    comment = ForumComment(post_id=post.id, parent_id=parent_id,
                           user_id=current_user.id, body=body,
                           anonymous=_wants_anonymous())
    db.session.add(comment)
    db.session.flush()
    from ..services.community_images import attach_images
    _img_saved, _img_skipped = attach_images(
        image_files, user=current_user, comment=comment)
    if not comment.anonymous:
        from ..services.social_graph import notify_mentions
        notify_mentions(current_user, body, post_id=post.id)
    db.session.commit()
    if _img_skipped:
        flash("Some images couldn't be added (max 4, images under 8 MB each).",
              "info")
    still_ok, exhausted_msg = can_free_reply(current_user)
    if not still_ok:
        flash(exhausted_msg, "info")
        from ..services.social_graph import notify
        notify(current_user.id, kind="quota", body=exhausted_msg,
               url=url_for("forums.post", post_id=post_id))
        db.session.commit()
    return redirect(url_for("forums.post", post_id=post_id) + "#comments")


@bp.route("/p/<int:post_id>/like", methods=["POST"])
@login_required
def like_post(post_id):
    post = db.session.get(ForumPost, post_id)
    if post is None or post.hidden:
        abort(404)
    blocked = _require_participant(url_for("forums.post", post_id=post_id))
    if blocked:
        return blocked
    existing = ForumPostLike.query.filter_by(user_id=current_user.id, post_id=post.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(ForumPostLike(user_id=current_user.id, post_id=post.id))
    db.session.commit()
    return redirect(request.form.get("next") or url_for("forums.post", post_id=post_id))


@bp.route("/comment/<int:comment_id>/like", methods=["POST"])
@login_required
def like_comment(comment_id):
    comment = db.session.get(ForumComment, comment_id)
    if comment is None or comment.hidden:
        abort(404)
    blocked = _require_participant(url_for("forums.post", post_id=comment.post_id))
    if blocked:
        return blocked
    existing = ForumCommentLike.query.filter_by(
        user_id=current_user.id, comment_id=comment.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(ForumCommentLike(user_id=current_user.id, comment_id=comment.id))
    db.session.commit()
    return redirect(request.form.get("next")
                    or url_for("forums.post", post_id=comment.post_id) + "#comments")


@bp.route("/p/<int:post_id>/report", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def report_post(post_id):
    from ..services.content_reports import submit_report
    note = (request.form.get("note") or "").strip()
    _report, msg = submit_report(reporter=current_user, target_type="post",
                                 target_id=post_id, note=note)
    flash(msg, "success" if _report else "error")
    post = db.session.get(ForumPost, post_id)
    if post is None or post.hidden:
        return redirect(url_for("forums.index"))
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("forums.post", post_id=post_id))


@bp.route("/comment/<int:comment_id>/report", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def report_comment(comment_id):
    from ..services.content_reports import submit_report
    note = (request.form.get("note") or "").strip()
    comment = db.session.get(ForumComment, comment_id)
    post_id = comment.post_id if comment else None
    _report, msg = submit_report(reporter=current_user, target_type="comment",
                                 target_id=comment_id, note=note)
    flash(msg, "success" if _report else "error")
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    if post_id:
        post = db.session.get(ForumPost, post_id)
        if post and not post.hidden:
            return redirect(url_for("forums.post", post_id=post_id) + "#comments")
    return redirect(url_for("forums.index"))


@bp.route("/p/<int:post_id>/delete", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def delete_own_post(post_id):
    """Authors (or Studio owners) can permanently remove a post and its comments."""
    from ..services.forum_moderation import delete_post

    post = db.session.get(ForumPost, post_id)
    if post is None:
        abort(404)
    is_owner = bool(getattr(current_user, "is_admin", False))
    if post.user_id != current_user.id and not is_owner:
        abort(404)
    cat_slug = post.category.slug if post.category else None
    delete_post(post)
    if is_owner and post.user_id != current_user.id:
        flash("Post removed.", "success")
    else:
        flash("Your post was deleted.", "success")
    if cat_slug:
        return redirect(url_for("forums.category", slug=cat_slug))
    return redirect(url_for("forums.index"))


@bp.route("/comment/<int:comment_id>/delete", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def delete_own_comment(comment_id):
    """Authors (or Studio owners) can permanently remove a comment and its replies."""
    from ..services.forum_moderation import delete_comment

    comment = db.session.get(ForumComment, comment_id)
    if comment is None:
        abort(404)
    is_owner = bool(getattr(current_user, "is_admin", False))
    if comment.user_id != current_user.id and not is_owner:
        abort(404)
    post_id = comment.post_id
    delete_comment(comment)
    if is_owner and comment.user_id != current_user.id:
        flash("Comment removed.", "success")
    else:
        flash("Your comment was deleted.", "success")
    post = db.session.get(ForumPost, post_id)
    if post and not post.hidden:
        return redirect(url_for("forums.post", post_id=post_id) + "#comments")
    return redirect(url_for("forums.index"))


@bp.route("/img/<int:image_id>")
def image(image_id):
    """Stream a post/comment image. Members only (the before_request gate),
    and never for content that is hidden or a room the viewer can't see."""
    from flask import Response

    img = db.session.get(ForumImage, image_id)
    if img is None or not img.data:
        abort(404)
    post = None
    if img.comment_id:
        comment = db.session.get(ForumComment, img.comment_id)
        if comment is None or comment.hidden:
            abort(404)
        post = db.session.get(ForumPost, comment.post_id)
    elif img.post_id:
        post = db.session.get(ForumPost, img.post_id)
    if post is None or post.hidden:
        abort(404)
    if not _can_access_category(post.category):
        abort(404)

    resp = Response(bytes(img.data), mimetype=(img.mime or "image/jpeg"))
    resp.headers["Cache-Control"] = "private, max-age=86400"
    resp.headers["Content-Length"] = str(len(img.data))
    return resp


def _liked_ids(post_ids, comment_ids):
    """Sets of post/comment ids the current member has liked (empty if anon)."""
    if not current_user.is_authenticated:
        return set(), set()
    liked_posts = set()
    liked_comments = set()
    if post_ids:
        liked_posts = {r.post_id for r in ForumPostLike.query.filter(
            ForumPostLike.user_id == current_user.id,
            ForumPostLike.post_id.in_(post_ids)).all()}
    if comment_ids:
        liked_comments = {r.comment_id for r in ForumCommentLike.query.filter(
            ForumCommentLike.user_id == current_user.id,
            ForumCommentLike.comment_id.in_(comment_ids)).all()}
    return liked_posts, liked_comments
