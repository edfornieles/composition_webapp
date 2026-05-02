from storages.backends.s3 import S3Storage


class StaticR2Storage(S3Storage):
    location = "static"
    default_acl = None
    file_overwrite = True


class MediaR2Storage(S3Storage):
    location = "media"
    default_acl = None
    file_overwrite = False

