"""Exercise the real FLAC adapter and pinned decoder with independently encoded PCM."""
from io import BytesIO
from pathlib import Path
import hashlib
import os
import subprocess
import tarfile
import urllib.request

import av
import numpy as np
import pytest

from tests.esphome_native import NATIVE_COMPONENTS

REVISION = 'f9c27d4d0166021db977d7fc37d205b21d81e9f2'


@pytest.mark.timeout(120)
def test_flac_adapter_fragments_metadata_eof_and_failed_file_cleanup(tmp_path):
    dependency = os.environ.get('MICRO_FLAC_REPO')
    if dependency:
        dependency = Path(dependency)
    else:
        url = f'https://codeload.github.com/esphome-libs/micro-flac/tar.gz/{REVISION}'
        with urllib.request.urlopen(url, timeout=30) as response:
            archive = BytesIO(response.read())
        with tarfile.open(fileobj=archive, mode='r:gz') as tar:
            selected = [member for member in tar.getmembers()
                        if '/include/' in member.name or '/src/' in member.name]
            tar.extractall(tmp_path, members=selected, filter='data')
        dependency = tmp_path / f'micro-flac-{REVISION}'
    decoder_source = dependency / 'src/flac_decoder.cpp'
    assert decoder_source.is_file()
    expected_digest = 'da8d09393c14ab66f262584a3dadaa183e765bcbda03c2f72e4b5762f98a7fce'
    assert hashlib.sha256(decoder_source.read_bytes()).hexdigest() == expected_digest

    pcm = ((np.arange(16000, dtype=np.int32) * 73) % 30000 - 15000).astype('<i2').reshape(1, -1)
    encoded = BytesIO()
    with av.open(encoded, 'w', format='flac') as container:
        stream = container.add_stream('flac', rate=16000)
        stream.layout = 'mono'
        frame = av.AudioFrame.from_ndarray(pcm, format='s16', layout='mono')
        frame.sample_rate = 16000
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    data = encoded.getvalue()
    assert data[:4] == b'fLaC' and not data[4] & 0x80
    streaminfo_end = 8 + int.from_bytes(data[5:8], 'big')
    assert streaminfo_end == 42
    (tmp_path / 'plain.flac').write_bytes(data)
    # A valid zero-length padding block, independent of our serializer/decoder.
    (tmp_path / 'zero-metadata.flac').write_bytes(data[:42] + b'\x01\x00\x00\x00' + data[42:])
    (tmp_path / 'reference.pcm').write_bytes(pcm.tobytes())
    (tmp_path / 'invalid.flac').write_bytes(b'BAD!' + data[4:])

    core = tmp_path / 'esphome/core'
    core.mkdir(parents=True)
    (core / 'defines.h').write_text('#pragma once\n')
    (core / 'helpers.h').write_text('''#pragma once
#include <cstring>
#include <strings.h>
#define ESPDEPRECATED(message, version)
namespace esphome {
inline bool str_endswith_ignore_case(const char *s, const char *suffix) {
  const size_t n=std::strlen(s), k=std::strlen(suffix);
  return n>=k && strcasecmp(s+n-k,suffix)==0;
}
}
''')
    source = (NATIVE_COMPONENTS / 'audio/audio_decoder.cpp').read_text()
    begin = source.index('FileDecoderState AudioDecoder::decode_flac_()')
    end = source.index('\n#endif', begin)
    body = source[begin:end]
    outer_begin = source.index('AudioDecoderState AudioDecoder::decode(bool')
    outer_end = source.index('\n#ifdef USE_AUDIO_FLAC_SUPPORT\nFileDecoderState', outer_begin)
    outer = source[outer_begin:outer_end]
    constants = source[source.index('static const uint32_t READ_WRITE_TIMEOUT_MS'):source.index('AudioDecoder::AudioDecoder(')]
    code = r'''
#include "audio.h"
#include <micro_flac/flac_decoder.h>
#include <algorithm>
#include <cassert>
#include <fstream>
#include <iterator>
#include <memory>
#include <optional>
#include <vector>
#define ESP_LOGE(...) ((void)0)
void delay(unsigned) {}
#define pdMS_TO_TICKS(x) (x)
namespace esphome::audio {
enum class AudioDecoderState { DECODING, FINISHED, FAILED };
enum class FileDecoderState { MORE_TO_PROCESS, IDLE, POTENTIALLY_FAILED, FAILED, END_OF_FILE };
struct Input {
  std::vector<uint8_t> bytes; size_t position=0, limit=0, chunk=4096;
  const uint8_t *data() const { return bytes.data()+position; }
  size_t available() const { return limit-position; }
  void consume(size_t n) { assert(n<=available()); position+=n; }
  bool has_buffered_data() const { return position<bytes.size(); }
  size_t fill(int) { if(position==limit)limit=std::min(bytes.size(),limit+chunk); return available(); }
};
struct Output {
  std::vector<uint8_t> delivered;
  std::vector<uint8_t> bytes=std::vector<uint8_t>(64); size_t used=0; bool fail=false;
  size_t available() const { return used; }
  size_t transfer_data_to_sink(int,bool) { delivered.insert(delivered.end(),bytes.begin(),bytes.begin()+used);size_t n=used;used=0;return n; }
  uint8_t *get_buffer_end() { return bytes.data()+used; }
  size_t free() const { return bytes.size()-used; }
  bool reallocate(size_t n) { if(fail)return false; bytes.resize(n); return true; }
  void increase_buffer_length(size_t n) { assert(n<=free()); used+=n; }
};
struct AudioDecoder {
  std::unique_ptr<micro_flac::FLACDecoder> flac_decoder_=std::make_unique<micro_flac::FLACDecoder>();
  std::unique_ptr<Input> input_buffer_=std::make_unique<Input>();
  std::unique_ptr<Output> output_transfer_buffer_=std::make_unique<Output>();
  std::optional<AudioStreamInfo> audio_stream_info_;
  size_t free_buffer_required_=0;
  bool end_of_file_=false, pause_output_=false;
  uint32_t potentially_failed_count_=0, accumulated_frames_written_=0, playback_ms_=0;
  AudioFileType audio_file_type_=AudioFileType::FLAC;
  FileDecoderState decode_flac_();
  AudioDecoderState decode(bool);
};
'''
    code += constants + outer + body + r'''
}
using namespace esphome::audio;
std::vector<uint8_t> read(const char *path) {
  std::ifstream f(path,std::ios::binary); assert(f);
  return {std::istreambuf_iterator<char>(f),std::istreambuf_iterator<char>()};
}
void valid(const char *path,const std::vector<uint8_t>& expected,size_t chunk) {
  AudioDecoder d; d.input_buffer_->bytes=read(path); d.input_buffer_->chunk=chunk;
  for(size_t iteration=0;iteration<100000;++iteration) {
    auto status=d.decode(true);
    assert(status!=AudioDecoderState::FAILED);
    if(status==AudioDecoderState::FINISHED) {
      assert(d.output_transfer_buffer_->delivered==expected);
      assert(d.playback_ms_==1000);
      return;
    }
  }
  assert(false && "decoder must make bounded progress to EOF");
}
int main(int argc,char **argv) {
  assert(argc==5);const auto expected=read(argv[3]);
  for(size_t chunk : {1U,7U,31U,128U,4096U}) {
    valid(argv[1],expected,chunk);valid(argv[2],expected,chunk);
  }
  {
    AudioDecoder failed;failed.input_buffer_->bytes=read(argv[4]);
    AudioDecoderState status=AudioDecoderState::DECODING;
    for(unsigned i=0;i<32 && status==AudioDecoderState::DECODING;++i)status=failed.decode(false);
    assert(status==AudioDecoderState::FAILED);
    assert(failed.output_transfer_buffer_->used==0);
  }
  {
    AudioDecoder failed;failed.input_buffer_->bytes=read(argv[1]);
    failed.output_transfer_buffer_->fail=true;
    AudioDecoderState status=AudioDecoderState::DECODING;
    for(unsigned i=0;i<32 && status==AudioDecoderState::DECODING;++i)status=failed.decode(false);
    assert(status==AudioDecoderState::FAILED);
  }
  // Fresh file lifetime after both failures, without stale decoder or output data.
  valid(argv[1],expected,31);
}
'''
    cpp = tmp_path / 'flac-adapter.cpp'
    cpp.write_text(code)
    binary = tmp_path / 'flac-adapter'
    sources = [dependency / 'src' / (name + '.cpp')
               for name in ('flac_decoder', 'crc', 'frame_header', 'pcm_packing', 'decorrelation', 'lpc')]
    command = ['g++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-DMICRO_FLAC_DISABLE_OGG',
               '-DUSE_AUDIO_FLAC_SUPPORT', '-I', str(tmp_path), '-I', str(dependency / 'include'),
               '-I', str(NATIVE_COMPONENTS / 'audio'), str(cpp),
               str(NATIVE_COMPONENTS / 'audio/audio.cpp'), *map(str, sources), '-o', str(binary)]
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    subprocess.run([str(binary), str(tmp_path / 'plain.flac'), str(tmp_path / 'zero-metadata.flac'),
                    str(tmp_path / 'reference.pcm'), str(tmp_path / 'invalid.flac')],
                   check=True, capture_output=True, text=True, timeout=10)
