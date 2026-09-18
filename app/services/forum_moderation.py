"""Studio / moderation helpers for forum posts and comments."""
from __future__ import annotations

from ..extensions import db
from ..models import (ContentReport, ForumComment, ForumCommentLike, ForumImage,
                      ForumPost, ForumPostLike, Notification)


def delete_post(post: ForumPost) -> None:
    """Remove a post and every row that would block the FK delete."""
    comment_ids = [
        cid for (cid,) in
        db.session.query(ForumComment.id).filter_by(post_id=post.id).all()
    ]

    # Attached images on the post and on any of its comments.
    ForumImage.query.filter_by(post_id=post.id).delete(synchronize_session=False)
    if comment_ids:
        ForumImage.query.filter(
            ForumImage.comment_id.in_(comment_ids)
        ).delete(synchronize_session=False)

    Notification.query.filter_by(post_id=post.id).delete(synchronize_session=False)
    ContentReport.query.filter_by(target_type="post", target_id=post.id).delete(
        synchronize_session=False
    )

    if comment_ids:
        ContentReport.query.filter(
            ContentReport.target_type == "comment",
            ContentReport.target_id.in_(comment_ids),
        ).delete(synchronize_session=False)
        ForumCommentLike.query.filter(
            ForumCommentLike.comment_id.in_(comment_ids)
        ).delete(synchronize_session=False)
        # Clear self-FK so reply rows can be removed in any order.
        ForumComment.query.filter(ForumComment.id.in_(comment_ids)).update(
            {ForumComment.parent_id: None}, synchronize_session=False
        )
        ForumComment.query.filter(ForumComment.id.in_(comment_ids)).delete(
            synchronize_session=False
        )

    ForumPostLike.query.filter_by(post_id=post.id).delete(synchronize_session=False)
    db.session.delete(post)
    db.session.commit()


def delete_comment(comment: ForumComment) -> None:
    """Remove a comment (and its one-level replies) without leaving orphans."""
    reply_ids = [
        rid for (rid,) in
        db.session.query(ForumComment.id).filter_by(parent_id=comment.id).all()
    ]
    all_ids = [comment.id] + reply_ids

    ForumImage.query.filter(
        ForumImage.comment_id.in_(all_ids)
    ).delete(synchronize_session=False)
    ContentReport.query.filter(
        ContentReport.target_type == "comment",
        ContentReport.target_id.in_(all_ids),
    ).delete(synchronize_session=False)
    ForumCommentLike.query.filter(
        ForumCommentLike.comment_id.in_(all_ids)
    ).delete(synchronize_session=False)
    if reply_ids:
        ForumComment.query.filter(ForumComment.id.in_(reply_ids)).delete(
            synchronize_session=False
        )
    db.session.delete(comment)
    db.session.commit()
