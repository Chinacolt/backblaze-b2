#!/usr/bin/python
#
# Author: Matthew Ingersoll <matth@mtingers.com>
#
# A class for accessing the Backblaze B2 API
#
# All of the API methods listed are implemented:
#   https://www.backblaze.com/b2/docs/
#
#

import os, sys, re, json, urllib2, base64, hashlib, mmap, threading, time
from mimetypes import MimeTypes
from Crypto import Random
from Crypto.Cipher import AES
import Queue

# Queue for multithreaded upload
QUEUE_SIZE = 48
upload_queue = Queue.Queue(maxsize=QUEUE_SIZE)

# Thanks to stackoverflow
# http://stackoverflow.com/questions/16761458/how-to-aes-encrypt-decrypt-files-using-python-pycrypto-in-an-openssl-compatible
# TODO: review if these encryption techniques are actually sound.
def derive_key_and_iv(password, salt, key_length, iv_length):
    d = d_i = ''
    while len(d) < key_length + iv_length:
        d_i = hashlib.md5(d_i + password + salt).digest()
        d += d_i
    return d[:key_length], d[key_length:key_length+iv_length]

def generate_salt_key_iv(password, key_length):
    bs = AES.block_size
    salt = Random.new().read(bs - len('Salted__'))
    key, iv = derive_key_and_iv(password, salt, key_length, bs)
    return (salt, key, iv) 

# A stupid way to calculate size of encrypted file and sha1
# B2 requires a header with the sha1 but urllib2 must have the header before streaming
# the data. This means we must read the file once to calculate the sha1, then read it again
# for streaming the data on upload.
def calc_encryption_sha_and_length(in_file, password, salt, key_length, key, iv):
    bs = AES.block_size
    size = 0
    cipher = AES.new(key, AES.MODE_CBC, iv) 
    sha = hashlib.sha1()
    sha.update('Salted__' + salt)
    size += len('Salted__' + salt)
    finished = False
    while not finished: 
        chunk = in_file.read(1024 * bs)
        if len(chunk) == 0 or len(chunk) % bs != 0:
            padding_length = (bs - len(chunk) % bs) or bs
            chunk += padding_length * chr(padding_length)
            finished = True
        chunk = cipher.encrypt(chunk)
        sha.update(chunk)
        size += len(chunk)
    return sha.hexdigest(), size

class Read2Encrypt(file):
    """ Return encrypted data from read() calls
        Override read() for urllib2 when streaming encrypted data (uploads)
    """
    def __init__(self, path, mode, password, salt, key_length, key, iv, size=0, *args):
        super(Read2Encrypt, self).__init__(path, mode)
        self.password = password
        self.bs = AES.block_size
        self.cipher = AES.new(key, AES.MODE_CBC, iv)
        (self.salt, self.key_length, self.key, self.iv) = (salt, key_length, key, iv)
        self.finished = False
        self._size = size
        self._args = args
        self.sha = None
        self.first_read = True

    def __len__(self):
        return self._size

    def read(self, size):
        if self.first_read:
            self.first_read = False
            return 'Salted__' + self.salt

        if self.finished:
            return None

        chunk = file.read(self, size)
        if len(chunk) == 0 or len(chunk) % self.bs != 0:
            padding_length = (self.bs - len(chunk) % self.bs) or self.bs
            chunk += padding_length * chr(padding_length)
            self.finished = True
            chunk = self.cipher.encrypt(chunk)
            return chunk
        if chunk:
            chunk = self.cipher.encrypt(chunk)
            return chunk

class BackBlazeB2(object):
    def __init__(self, account_id, app_key):
        self.account_id = account_id
        self.app_key = app_key
        self.authorization_token = None
        self.api_url = None
        self.download_url = None
        self.upload_url = None
        self.upload_authorization_token = None

    def authorize_account(self):
        id_and_key = self.account_id+':'+self.app_key
        basic_auth_string = 'Basic ' + base64.b64encode(id_and_key)
        headers = { 'Authorization': basic_auth_string }
        try:
            request = urllib2.Request(
                'https://api.backblaze.com/b2api/v1/b2_authorize_account',
                headers = headers
            )
            response = urllib2.urlopen(request)
            response_data = json.loads(response.read())
            response.close()
        except urllib2.HTTPError, error:
            print("ERROR: %s" %  error.read())
            raise

        self.authorization_token = response_data['authorizationToken']
        self.api_url = response_data['apiUrl']
        self.download_url = response_data['downloadUrl']
        return response_data

    def _authorize_account(self):
        if not self.authorization_token or not self.api_url:
            self.authorize_account()

    def create_bucket(self, bucket_name, bucket_type='allPrivate'):
        self._authorize_account()
        # bucket_type can be Either allPublic or allPrivate
        return self._api_request('%s/b2api/v1/b2_create_bucket' % self.api_url,
            { 'accountId': self.account_id, 'bucketName': bucket_name, 'bucketType': bucket_type},
            { 'Authorization': self.authorization_token })

    def list_buckets(self):
        self._authorize_account()
        return self._api_request('%s/b2api/v1/b2_list_buckets' % self.api_url,
            { 'accountId' : self.account_id },
            { 'Authorization': self.authorization_token })

    def get_bucket_info(self, bucket_id, bucket_name):
        bkt = None
        if not bucket_id and not bucket_name:
            raise Exception("create_bucket requires either a bucket_id or bucket_name")
        if bucket_id and bucket_name:
            raise Exception("create_bucket requires only _one_ argument and not both bucket_id and bucket_name")

        buckets = self.list_buckets()['buckets']
        if not bucket_id:
            key = 'bucketName'
            val = bucket_name
        else:
            key = 'bucketId'
            val = bucket_id
        for bucket in buckets:
            if bucket[key] == val:
                bkt = bucket
                break
        return bkt

    def delete_bucket(self, bucket_id=None, bucket_name=None):
        if not bucket_id and not bucket_name:
            raise Exception("create_bucket requires either a bucket_id or bucket_name")
        if bucket_id and bucket_name:
            raise Exception("create_bucket requires only _one_ argument and not both bucket_id and bucket_name")
        self._authorize_account()
        bucket = self.get_bucket_info(bucket_id, bucket_name)
        return self._api_request('%s/b2api/v1/b2_delete_bucket' % self.api_url,
            { 'accountId': self.account_id, 'bucketId': bucket['bucketId'] },
            { 'Authorization': self.authorization_token })

    def get_upload_url(self, bucket_name, bucket_id):
        self._authorize_account()
        bucket = self.get_bucket_info(bucket_id, bucket_name)
        bucket_id = bucket['bucketId']
        return self._api_request('%s/b2api/v1/b2_get_upload_url' % self.api_url,
            { 'bucketId' : bucket_id },
            { 'Authorization': self.authorization_token })

    def upload_file(self, path, bucket_id=None, bucket_name=None):
        self._authorize_account()

        # use mmap for streaming the data to avoid storing the entire file in memory
        fp = open(path, 'rb')
        mm_file_data = mmap.mmap(fp.fileno(), 0, access=mmap.ACCESS_READ)
        mime = MimeTypes()
        mime_type = mime.guess_type(path)
        if not self.upload_url or not self.upload_authorization_token:
            url = self.get_upload_url(bucket_name=bucket_name, bucket_id=bucket_id)
            self.upload_url = url['uploadUrl']
            self.upload_authorization_token = url['authorizationToken']
        filename = re.sub('^/', '', path)
        filename = re.sub('//', '/', filename)
        # TODO: Figure out URL encoding issue
        #filename = unicode(filename, "utf-8")
        content_type = mime_type
        sha = hashlib.sha1()
        with open(path, 'rb') as f:
            while True:
                block = f.read(2**10)
                if not block: break
                sha.update(block)
        sha1_of_file_data = sha.hexdigest()
        headers = {
            'Authorization' : self.upload_authorization_token,
            'X-Bz-File-Name' :  filename,
            'Content-Type' : content_type,
            'X-Bz-Content-Sha1' : sha1_of_file_data
        }
        request = urllib2.Request(self.upload_url, mm_file_data, headers)

        response = urllib2.urlopen(request)
        response_data = json.loads(response.read())
        response.close()
        fp.close()
        return response_data

    # Encrypt files that are uploaded
    def upload_file_encrypt(self, path, password, bucket_id=None, bucket_name=None):
        self._authorize_account()

        (salt, key, iv) = generate_salt_key_iv(password, 32)
        in_file = open(path, 'rb')
        (sha, size) = calc_encryption_sha_and_length(in_file, password, salt, 32, key, iv)
        in_file.close()

        fp = Read2Encrypt(path, 'rb', password, salt, 32, key, iv, size=size)

        if not self.upload_url or not self.upload_authorization_token:
            url = self.get_upload_url(bucket_name=bucket_name, bucket_id=bucket_id)
            self.upload_url = url['uploadUrl']
            self.upload_authorization_token = url['authorizationToken']

        # fixup filename
        filename = re.sub('^/', '', path)
        filename = re.sub('//', '/', filename)
        # TODO: Figure out URL encoding issue
        #filename = unicode(filename, "utf-8")
        headers = {
            'Authorization' : self.upload_authorization_token,
            'X-Bz-File-Name' : filename,
            'Content-Type' : 'application/octet-stream',
            'X-Bz-Content-Sha1' : sha
        }
        try:
            request = urllib2.Request(self.upload_url, fp, headers)
            response = urllib2.urlopen(request)
            response_data = json.loads(response.read())
        except urllib2.HTTPError, error:
            print("ERROR: %s" %  error.read())
            raise

        response.close()
        fp.close()
        return response_data

    def update_bucket(self, bucket_type, bucket_id=None, bucket_name=None):
        if bucket_type not in ('allPublic', 'allPrivate'):
            raise Exception("update_bucket: Invalid bucket_type.  Must be string allPublic or allPrivate")

        bucket = self.get_bucket_info(bucket_id=bucket_id, bucket_name=bucket_name)
        return self._api_request('%s/b2api/v1/b2_update_bucket' % self.api_url,
            { 'bucketId' : bucket['bucketId'], 'bucketType' : bucket_type },
            { 'Authorization': self.authorization_token })

    def list_file_versions(self, bucket_id=None, bucket_name=None):
        bucket = self.get_bucket_info(bucket_id=bucket_id, bucket_name=bucket_name)
        return self._api_request('%s/b2api/v1/b2_list_file_versions' % self.api_url,
            { 'bucketId' : bucket['bucketId'] },
            { 'Authorization': self.authorization_token })

    def list_file_names(self, bucket_id=None, bucket_name=None):
        bucket = self.get_bucket_info(bucket_id=bucket_id, bucket_name=bucket_name)
        return self._api_request('%s/b2api/v1/b2_list_file_names' % self.api_url,
            { 'bucketId' : bucket['bucketId'] },
            { 'Authorization': self.authorization_token })

    def hide_file(self, file_name, bucket_id=None, bucket_name=None):
        bucket = self.get_bucket_info(bucket_id=bucket_id, bucket_name=bucket_name)
        return self._api_request('%s/b2api/v1/b2_list_file_versions' % self.api_url,
            { 'bucketId' : bucket['bucketId'], 'fileName' : file_name },
            { 'Authorization': self.authorization_token })

    def delete_file_version(self, file_name, file_id):
        bucket = self.get_bucket_info(bucket_id=bucket_id, bucket_name=bucket_name)
        return self._api_request('%s/b2api/v1/b2_delete_file_version' % self.api_url,
            { 'fileName' : file_name, 'fileId': file_id },
            { 'Authorization': self.authorization_token })

    def get_file_info_by_name(self, file_name, bucket_id=None, bucket_name=None):
        file_names = self.list_file_names(bucket_id=bucket_id, bucket_name=bucket_name)
        for i in file_names['files']:
            if i['fileName'] == file_name:
                return self.get_file_info(i['fileId'])
        return None

    def get_file_info(self, file_id):
        return self._api_request('%s/b2api/v1/b2_get_file_info' % self.api_url,
            { 'fileId' : file_id  },
            { 'Authorization': self.authorization_token })

    def download_file_by_name(self, file_name, dst_file_name, bucket_id=None, bucket_name=None, force=False):
        if os.path.exists(dst_file_name) and not force:
            raise Exception("Destination file exists. Refusing to overwrite. Set force=True if you wish to do so.")

        self._authorize_account()
        bucket = self.get_bucket_info(bucket_id=bucket_id, bucket_name=bucket_name)
        url = self.download_url + '/file/' + bucket['bucketName'] + '/' + file_name
        request = urllib2.Request(url, None, { 'Authorization': self.authorization_token })
        resp = urllib2.urlopen(request)
        with open(dst_file_name, 'wb') as f:
            while True:
                chunk = resp.read(2**10)
                if not chunk: break
                f.write(chunk)
        return True

    def download_file_by_id(self, file_name, dst_file_name, force=False):
        if os.path.exists(dst_file_name) and not force:
            raise Exception("Destination file exists. Refusing to overwrite. Set force=True if you wish to do so.")

        self._authorize_account()
        url = self.download_url + '/b2api/v1/b2_download_file_by_id?fileId=' + file_id
        request = urllib2.Request(url, None, { 'Authorization': self.authorization_token })
        resp = urllib2.urlopen(request)
        with open(dst_file_name, 'wb') as f:
            while True:
                chunk = resp.read(2**10)
                if not chunk: break
                f.write(chunk)
        return True

    def recursive_upload(self, path, bucket_id=None, bucket_name=None, exclude_regex=None, include_regex=None,
            exclude_re_flags=None, include_re_flags=None, password=None):
        bucket = self.get_bucket_info(bucket_id=bucket_id, bucket_name=bucket_name)
        if exclude_regex:
            exclude_regex = re.compile(exclude_regex, flags=exclude_re_flags)
        if include_regex:
            include_regex = re.compile(include_regex, flags=include_re_flags)

        nfiles = 0
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for f in files:
                    if os.path.islink(root+'/'+f): continue
                    if exclude_regex and exclude_regex.match(root+'/'+f): continue
                    if include_regex and not include_regex.match(root+'/'+f): continue
                    print("UPLOAD: %s" % root+'/'+f)
                    if password:
                        self.upload_file_encrypt(root+'/'+f, password, bucket_id=bucket_id, bucket_name=bucket_name)
                    else:
                        self.upload_file(root+'/'+f,  bucket_id=bucket_id, bucket_name=bucket_name)
                    nfiles += 1
        else:
            nfiles = 1
            if not os.path.islink(path):
                if exclude_regex and exclude_regex.match(path):
                    nfiles -= 1
                if include_regex and include_regex.match(path):
                    nfiles += 1
            if nfiles > 0:
                print("UPLOAD: %s" % path)
                if password:
                    self.upload_file_encrypt(path, password, bucket_id=bucket_id, bucket_name=bucket_name)
                else:
                    self.upload_file(path,  bucket_id=bucket_id, bucket_name=bucket_name)
                return 1
            else:
                print("WARNING: No files uploaded")
        return nfiles


    def upload_worker(self, password, bucket_id, bucket_name):
        while not self.upload_queue_done:
            time.sleep(1)
            try:
                path = upload_queue.get_nowait()
            except:
                continue
            # try a few times in case of error
            for i in range(4):
                try:
                    if password:
                        self.upload_file_encrypt(path, password, bucket_id=bucket_id, bucket_name=bucket_name)
                    else:
                        self.upload_file(path, bucket_id=bucket_id, bucket_name=bucket_name)
                    break
                except Exception, e:
                    print("WARNING: Error processing file '%s'\n%s\nTrying again." % (path, e))
                    time.sleep(1)

    def recursive_upload_mt(self, path, bucket_id=None, bucket_name=None, exclude_regex=None, include_regex=None,
            exclude_re_flags=None, include_re_flags=None, password=None):
        bucket = self.get_bucket_info(bucket_id=bucket_id, bucket_name=bucket_name)
        if exclude_regex:
            exclude_regex = re.compile(exclude_regex, flags=exclude_re_flags)
        if include_regex:
            include_regex = re.compile(include_regex, flags=include_re_flags)

        nfiles = 0
        if os.path.isdir(path):
            # Generate Queue worker threads to match QUEUE_SIZE
            self.threads = []
            self.upload_queue_done = False
            for i in range(QUEUE_SIZE):
                t = threading.Thread(target=self.upload_worker, args=(password, bucket_id, bucket_name,))
                self.threads.append(t)
                t.start()

            for root, dirs, files in os.walk(path):
                for f in files:
                    if os.path.islink(root+'/'+f): continue
                    if exclude_regex and exclude_regex.match(root+'/'+f): continue
                    if include_regex and not include_regex.match(root+'/'+f): continue
                    print("UPLOAD: %s" % root+'/'+f)
                    upload_queue.put(root+'/'+f)
                    nfiles += 1
            self.upload_queue_done = True
            for t in self.threads:
                t.join()
 
        else:
            nfiles = 1
            if not os.path.islink(path):
                if exclude_regex and exclude_regex.match(path):
                    nfiles -= 1
                if include_regex and include_regex.match(path):
                    nfiles += 1
            if nfiles > 0:
                print("UPLOAD: %s" % path)
                if password:
                    self.upload_file_encrypt(path, password, bucket_id=bucket_id, bucket_name=bucket_name)
                else:
                    self.upload_file(path,  bucket_id=bucket_id, bucket_name=bucket_name)
                return 1
            else:
                print("WARNING: No files uploaded")
        return nfiles

    def _api_request(self, url, data, headers):
        self._authorize_account()
        request = urllib2.Request(url, json.dumps(data), headers)
        response = urllib2.urlopen(request)
        response_data = json.loads(response.read())
        response.close()
        return response_data

if __name__ == "__main__":
    # usage: <accountid> <appkey> <path> <bucketname>
    b2 = BackBlazeB2(sys.argv[1], sys.argv[2])
    b2.recursive_upload_mt(sys.arv[3], bucket_name=sys.argv[4])
