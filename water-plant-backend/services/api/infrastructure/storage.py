"""Private document storage. Downloads are authorized first, then served by a
short-lived signed URL. Filenames are display metadata, never storage paths."""

from pathlib import Path

from django.conf import settings
from django.core import signing

SIGNER_SALT = "document-download"


class LocalStorage:
    def __init__(self):
        self.root = Path(settings.DOCUMENT_LOCAL_ROOT)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("invalid storage key")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def upload_target(self, document) -> dict:
        return {"method": "PUT", "url": f"/api/v1/documents/{document.id}/content", "headers": {
            "Content-Type": document.content_type}}

    def download_url(self, document) -> str:
        token = signing.dumps({"d": str(document.id)}, salt=SIGNER_SALT)
        return f"/api/v1/documents/{document.id}/blob?token={token}"


class S3Storage:
    def __init__(self):
        import boto3  # optional dependency: pip install .[s3]

        self.client = boto3.client("s3", endpoint_url=settings.DOCUMENT_S3_ENDPOINT_URL)
        self.bucket = settings.DOCUMENT_S3_BUCKET

    def put(self, key, data, content_type):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def upload_target(self, document):
        url = self.client.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": document.storage_key,
                                  "ContentType": document.content_type},
            ExpiresIn=settings.DOCUMENT_DOWNLOAD_TTL_SECONDS)
        return {"method": "PUT", "url": url, "headers": {"Content-Type": document.content_type}}

    def download_url(self, document):
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": document.storage_key,
                                  "ResponseContentDisposition": f'attachment; filename="{document.display_filename}"'},
            ExpiresIn=settings.DOCUMENT_DOWNLOAD_TTL_SECONDS)


def get_storage():
    return S3Storage() if settings.DOCUMENT_STORAGE_BACKEND == "s3" else LocalStorage()


def read_download_token(token: str):
    return signing.loads(token, salt=SIGNER_SALT, max_age=settings.DOCUMENT_DOWNLOAD_TTL_SECONDS)["d"]
