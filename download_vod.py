import bisect
import copy
import json
import math
import os
import random
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

import httplib2
import m3u8
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from m3u8.model import SegmentList


CHANNEL = os.environ.get("KICK_CHANNEL", "vector")
KICK_API_URL = f"https://kick.com/api/v2/channels/{CHANNEL}/videos"

WORKSPACE = Path(os.environ.get("WORKSPACE_DIR", "/mnt/workspace"))

PRIVACY_STATUS = os.environ.get(
    "YOUTUBE_PRIVACY_STATUS",
    "private",
)

CATEGORY_ID = os.environ.get(
    "YOUTUBE_CATEGORY_ID",
    "20",
)

# Pausa entre subidas de partes del VOD.
# Por defecto: 60 segundos.
UPLOAD_PAUSE_SECONDS = int(
    os.environ.get(
        "UPLOAD_PAUSE_SECONDS",
        "7200",
    )
)

# 11 horas y 50 minutos.
# Margen para evitar acercarse al límite de 12 horas de YouTube.
MAX_PART_DURATION_SECONDS = int(
    os.environ.get(
        "MAX_PART_DURATION_SECONDS",
        str(11 * 60 * 60 + 50 * 60),
    )
)


HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) "
        "Gecko/20100101 Firefox/152.0"
    ),
    "Accept": (
        "application/json,"
        "application/vnd.apple.mpegurl,"
        "*/*"
    ),
}


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


RETRIABLE_STATUS_CODES = {
    500,
    502,
    503,
    504,
}


RETRIABLE_EXCEPTIONS = (
    httplib2.HttpLib2Error,
    IOError,
    socket.timeout,
)


MAX_RETRIES = 10


LEGACY_VOD_TAG_PATTERN = re.compile(
    r"kick-vod-id:([^\s]+)"
)


PART_TAG_PATTERN = re.compile(
    r"kick-vod-(?P<vod_id>.+)-part-(?P<part>\d+)-of-(?P<total>\d+)"
)



def get_required_env(name):
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"Falta la variable o secreto requerido: {name}"
        )

    return value



def build_youtube_client():

    credentials = Credentials(
        token=None,
        refresh_token=get_required_env(
            "YOUTUBE_REFRESH_TOKEN"
        ),
        token_uri=(
            "https://oauth2.googleapis.com/token"
        ),
        client_id=get_required_env(
            "YOUTUBE_CLIENT_ID"
        ),
        client_secret=get_required_env(
            "YOUTUBE_CLIENT_SECRET"
        ),
        scopes=SCOPES,
    )

    return build(
        "youtube",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

def get_uploaded_markers(youtube):
    """
    Obtiene los VOD completos antiguos y las partes nuevas ya subidas.
    """

    channel_response = youtube.channels().list(
        part="contentDetails",
        mine=True,
    ).execute()

    if not channel_response.get("items"):
        raise RuntimeError(
            "No se encontró un canal de YouTube para estas credenciales."
        )

    uploads_playlist = (
        channel_response["items"][0]
        ["contentDetails"]
        ["relatedPlaylists"]
        ["uploads"]
    )

    legacy_vod_ids = set()
    uploaded_part_tags = set()

    page_token = None

    while True:

        playlist_response = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist,
            maxResults=50,
            pageToken=page_token,
        ).execute()


        video_ids = [
            item["contentDetails"]["videoId"]
            for item in playlist_response.get(
                "items",
                []
            )
        ]


        if video_ids:

            videos_response = youtube.videos().list(
                part="snippet",
                id=",".join(video_ids),
                maxResults=50,
            ).execute()


            for item in videos_response.get(
                "items",
                []
            ):

                snippet = item.get(
                    "snippet",
                    {}
                )

                description = snippet.get(
                    "description",
                    ""
                )

                tags = snippet.get(
                    "tags",
                    []
                )


                match = LEGACY_VOD_TAG_PATTERN.search(
                    description
                )

                if match:
                    legacy_vod_ids.add(
                        match.group(1)
                    )


                for tag in tags:

                    legacy_match = (
                        LEGACY_VOD_TAG_PATTERN.fullmatch(tag)
                    )

                    if legacy_match:
                        legacy_vod_ids.add(
                            legacy_match.group(1)
                        )


                    if PART_TAG_PATTERN.fullmatch(tag):
                        uploaded_part_tags.add(tag)


        page_token = playlist_response.get(
            "nextPageToken"
        )


        if not page_token:
            break


    return (
        legacy_vod_ids,
        uploaded_part_tags,
    )



def get_kick_videos():

    request = urllib.request.Request(
        KICK_API_URL,
        headers=HTTP_HEADERS,
    )


    with urllib.request.urlopen(
        request,
        timeout=60,
    ) as response:

        videos = json.load(response)


    if not isinstance(videos, list):

        raise RuntimeError(
            "La API de Kick no devolvió una lista de videos."
        )


    return sorted(
        (
            video
            for video in videos
            if video.get("is_live") is not True
            and video.get("source")
            and video.get("id") is not None
        ),
        key=lambda video: (
            video.get(
                "created_at",
                ""
            ),
            str(
                video.get(
                    "id",
                    ""
                )
            ),
        ),
    )



def fetch_playlist(url):
    """
    Descarga y analiza un playlist,
    conservando la URL final tras redirecciones.
    """

    request = urllib.request.Request(
        url,
        headers=HTTP_HEADERS,
    )


    with urllib.request.urlopen(
        request,
        timeout=60,
    ) as response:

        final_url = response.geturl()

        content = response.read().decode(
            "utf-8-sig"
        )


    return (
        m3u8.loads(
            content,
            uri=final_url,
        ),
        final_url,
    )



def variant_score(variant):
    """
    Prioriza ancho de banda,
    resolución y FPS para elegir
    la mejor variante.
    """

    stream_info = variant.stream_info


    bandwidth = (
        getattr(
            stream_info,
            "average_bandwidth",
            None,
        )
        or getattr(
            stream_info,
            "bandwidth",
            None,
        )
        or 0
    )


    resolution = (
        getattr(
            stream_info,
            "resolution",
            None,
        )
        or (0, 0)
    )


    frame_rate = (
        getattr(
            stream_info,
            "frame_rate",
            None,
        )
        or 0
    )


    return (
        int(bandwidth),
        int(resolution[0])
        * int(resolution[1]),
        float(frame_rate),
    )

def resolve_media_playlist(source_url):
    """
    Sigue playlists maestros y selecciona la variante
    de mayor calidad.
    """

    current_url = source_url


    for _ in range(4):

        playlist, final_url = fetch_playlist(
            current_url
        )


        if not playlist.is_variant:

            if not playlist.segments:
                raise RuntimeError(
                    f"El playlist no contiene segmentos multimedia: {final_url}"
                )

            return (
                playlist,
                final_url,
            )


        if not playlist.playlists:
            raise RuntimeError(
                f"El playlist maestro no contiene variantes: {final_url}"
            )


        selected_variant = max(
            playlist.playlists,
            key=variant_score,
        )


        current_url = urljoin(
            final_url,
            selected_variant.uri,
        )


        stream_info = selected_variant.stream_info


        resolution = getattr(
            stream_info,
            "resolution",
            None,
        )

        frame_rate = getattr(
            stream_info,
            "frame_rate",
            None,
        )


        bandwidth = (
            getattr(
                stream_info,
                "average_bandwidth",
                None,
            )
            or getattr(
                stream_info,
                "bandwidth",
                None,
            )
        )


        print(
            "Variante HLS seleccionada: "
            f"{resolution or 'resolución desconocida'}, "
            f"{frame_rate or 'FPS desconocidos'} FPS, "
            f"{bandwidth or 'bitrate desconocido'} bps"
        )


    raise RuntimeError(
        "Se encontraron demasiados playlists maestros anidados."
    )



def build_balanced_segment_groups(
    segments,
    max_duration,
):
    """
    Divide segmentos consecutivos en partes de duración similar.

    Los cortes siempre ocurren entre segmentos HLS
    y ninguna parte supera max_duration.
    """

    durations = [
        float(segment.duration or 0)
        for segment in segments
    ]


    if (
        not durations
        or any(duration <= 0 for duration in durations)
    ):
        raise RuntimeError(
            "El playlist contiene segmentos sin duración válida."
        )


    longest_segment = max(durations)


    if longest_segment > max_duration:

        raise RuntimeError(
            "Un único segmento HLS supera "
            "la duración máxima permitida: "
            f"{longest_segment:.3f} segundos."
        )


    cumulative = [0.0]


    for duration in durations:
        cumulative.append(
            cumulative[-1] + duration
        )


    total_duration = cumulative[-1]


    initial_part_count = max(
        1,
        math.ceil(
            total_duration / max_duration
        ),
    )


    for part_count in range(
        initial_part_count,
        len(segments) + 1,
    ):

        boundaries = [0]

        start_index = 0

        valid = True


        for boundary_number in range(
            1,
            part_count,
        ):

            remaining_parts = (
                part_count - boundary_number
            )

            maximum_index = (
                len(segments)
                - remaining_parts
            )


            maximum_cumulative = (
                cumulative[start_index]
                + max_duration
            )


            minimum_cumulative = (
                total_duration
                - remaining_parts * max_duration
            )


            minimum_index = max(
                start_index + 1,
                bisect.bisect_left(
                    cumulative,
                    minimum_cumulative,
                    start_index + 1,
                    maximum_index + 1,
                ),
            )


            maximum_valid_index = (
                bisect.bisect_right(
                    cumulative,
                    maximum_cumulative,
                    minimum_index,
                    maximum_index + 1,
                )
                - 1
            )


            if minimum_index > maximum_valid_index:
                valid = False
                break


            target_cumulative = (
                total_duration
                * boundary_number
                / part_count
            )


            insertion_index = (
                bisect.bisect_left(
                    cumulative,
                    target_cumulative,
                    minimum_index,
                    maximum_valid_index + 1,
                )
            )


            candidates = {
                min(
                    max(
                        insertion_index,
                        minimum_index,
                    ),
                    maximum_valid_index,
                ),
                min(
                    max(
                        insertion_index - 1,
                        minimum_index,
                    ),
                    maximum_valid_index,
                ),
            }


            selected_index = min(
                candidates,
                key=lambda index:
                    abs(
                        cumulative[index]
                        - target_cumulative
                    ),
            )


            boundaries.append(
                selected_index
            )

            start_index = selected_index


        if not valid:
            continue


        boundaries.append(
            len(segments)
        )


        groups = []


        for part_index in range(
            len(boundaries) - 1
        ):

            start = boundaries[part_index]

            end = boundaries[part_index + 1]

            duration = (
                cumulative[end]
                - cumulative[start]
            )


            groups.append(
                {
                    "start": start,
                    "end": end,
                    "duration": duration,
                }
            )


        if (
            groups
            and all(
                group["duration"] <= max_duration
                for group in groups
            )
        ):
            return (
                groups,
                total_duration,
            )


    raise RuntimeError(
        "No se pudo dividir el playlist dentro del límite configurado."
    )

def absolutize_segment_references(
    segment,
    playlist_url,
):
    segment.uri = urljoin(
        playlist_url,
        segment.uri,
    )


    if segment.key and segment.key.uri:
        segment.key.uri = urljoin(
            playlist_url,
            segment.key.uri,
        )


    if (
        segment.init_section
        and segment.init_section.uri
    ):
        segment.init_section.uri = urljoin(
            playlist_url,
            segment.init_section.uri,
        )


    for partial_segment in (
        getattr(segment, "parts", [])
        or []
    ):

        if partial_segment.uri:

            partial_segment.uri = urljoin(
                playlist_url,
                partial_segment.uri,
            )



def write_part_playlist(
    media_playlist,
    media_playlist_url,
    start_index,
    end_index,
    destination,
):
    """
    Crea un playlist HLS local conservando
    KEY, MAP, BYTERANGE, DISCONTINUITY
    y metadatos asociados.
    """

    part_playlist = copy.deepcopy(
        media_playlist
    )


    selected_segments = list(
        part_playlist.segments[
            start_index:end_index
        ]
    )


    if not selected_segments:
        raise RuntimeError(
            "Se intentó crear una parte HLS sin segmentos."
        )


    for segment in selected_segments:

        absolutize_segment_references(
            segment,
            media_playlist_url,
        )


    part_playlist.segments = SegmentList(
        selected_segments
    )


    part_playlist.media_sequence = (
        int(
            media_playlist.media_sequence
            or 0
        )
        + start_index
    )


    part_playlist.is_endlist = True

    part_playlist.playlist_type = "vod"



    for attribute in (
        "preload_hint",
        "server_control",
        "skip",
        "rendition_reports",
        "start",
    ):

        if hasattr(
            part_playlist,
            attribute,
        ):

            setattr(
                part_playlist,
                attribute,
                None,
            )


    destination.write_text(
        part_playlist.dumps(),
        encoding="utf-8",
    )



def download_part(
    part_playlist_path,
    output_path,
):
    """
    Descarga y concatena los segmentos
    de una parte sin recodificar.
    """

    subprocess.run(
        [
            "ffmpeg",

            "-protocol_whitelist",
            "file,http,https,tcp,tls,crypto,data",

            "-i",
            str(part_playlist_path),

            "-c",
            "copy",

            str(output_path),
        ],
        check=True,
    )



def format_seconds(seconds):

    rounded = int(
        round(seconds)
    )


    hours, remainder = divmod(
        rounded,
        3600,
    )


    minutes, secs = divmod(
        remainder,
        60,
    )


    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d}"
    )



def make_part_tag(
    video_id,
    part_number,
    total_parts,
):

    return (
        f"kick-vod-{video_id}"
        f"-part-{part_number}"
        f"-of-{total_parts}"
    )



def has_complete_part_set(
    video_id,
    uploaded_part_tags,
):
    """
    Permite omitir VODs ya completos
    sin volver a consultar su playlist.
    """

    parts_by_total = {}


    for tag in uploaded_part_tags:

        match = PART_TAG_PATTERN.fullmatch(
            tag
        )


        if (
            not match
            or match.group("vod_id")
            != video_id
        ):
            continue


        part = int(
            match.group("part")
        )


        total = int(
            match.group("total")
        )


        parts_by_total.setdefault(
            total,
            set(),
        ).add(part)



    return any(
        parts == set(
            range(
                1,
                total + 1,
            )
        )

        for total, parts
        in parts_by_total.items()
    )

def build_video_metadata(
    video,
    part_number,
    total_parts,
):

    video_id = str(
        video["id"]
    )


    created_at = str(
        video.get(
            "created_at",
            "",
        )
    )


    video_date = (
        created_at
        .split("T")[0]
        .split(" ")[0]
    )


    try:

        year, month, day = video_date.split("-")

        formatted_date = (
            f"{day}/{month}/{year}"
        )

    except ValueError as error:

        raise RuntimeError(
            f"Fecha created_at no válida "
            f"para el VOD {video_id}: {created_at}"
        ) from error



    channel_name = (
        CHANNEL[:1].upper()
        + CHANNEL[1:]
    )


    session_title = str(
        video.get(
            "session_title",
            "Sin título",
        )
    )


    start_time = str(
        video.get(
            "start_time",
            "",
        )
    )


    source = str(
        video.get(
            "source",
            "",
        )
    )


    base_title = (
        f"{channel_name} | {formatted_date}"
    )


    if total_parts > 1:

        title = (
            f"{base_title} | "
            f"{part_number}/{total_parts}"
        )

    else:

        title = base_title



    description = "\n\n".join(
        [
            session_title,
            start_time,
            source,
        ]
    )


    part_tag = make_part_tag(
        video_id,
        part_number,
        total_parts,
    )


    return (
        title[:100],
        description,
        part_tag,
    )



def upload_video(
    youtube,
    video,
    video_path,
    part_number,
    total_parts,
):

    title, description, part_tag = (
        build_video_metadata(
            video,
            part_number,
            total_parts,
        )
    )



    insert_request = youtube.videos().insert(
        part="snippet,status",

        notifySubscribers=False,

        body={
            "snippet": {
                "title": title,

                "description": description,

                "tags": [
                    CHANNEL,
                    "Kick",
                    "VOD",
                    part_tag,
                ],

                "categoryId": CATEGORY_ID,
            },

            "status": {
                "privacyStatus": PRIVACY_STATUS,
            },
        },


        media_body=MediaFileUpload(
            str(video_path),

            mimetype="video/mp2t",

            chunksize=8 * 1024 * 1024,

            resumable=True,
        ),
    )



    response = None

    retry = 0



    while response is None:

        try:

            status, response = (
                insert_request.next_chunk()
            )


            if status:

                print(
                    "Progreso de subida: "
                    f"{int(status.progress() * 100)}%"
                )



        except HttpError as error:


            if (
                error.resp.status
                not in RETRIABLE_STATUS_CODES
            ):

                raise



            retry += 1



            if retry > MAX_RETRIES:

                raise RuntimeError(
                    "Se agotaron los reintentos de subida."
                ) from error



            delay = (
                random.random()
                * (2 ** retry)
            )


            print(
                f"Error temporal HTTP "
                f"{error.resp.status}; "
                f"nuevo intento en "
                f"{delay:.1f} segundos."
            )


            time.sleep(
                delay
            )



        except RETRIABLE_EXCEPTIONS as error:


            retry += 1



            if retry > MAX_RETRIES:

                raise RuntimeError(
                    "Se agotaron los reintentos de subida."
                ) from error



            delay = (
                random.random()
                * (2 ** retry)
            )


            print(
                "Error temporal de conexión; "
                f"nuevo intento en "
                f"{delay:.1f} segundos: {error}"
            )


            time.sleep(
                delay
            )



    uploaded_id = response["id"]


    print(
        "Video subido correctamente: "
        f"https://youtu.be/{uploaded_id}"
    )


    return (
        uploaded_id,
        part_tag,
    )

def process_oldest_pending_video(
    youtube,
    videos,
    legacy_vod_ids,
    uploaded_part_tags,
):

    for video in videos:

        video_id = str(
            video["id"]
        )


        if video_id in legacy_vod_ids:
            continue


        if has_complete_part_set(
            video_id,
            uploaded_part_tags,
        ):
            continue



        print(
            f"Analizando el VOD pendiente más antiguo: {video_id}"
        )



        media_playlist, media_playlist_url = (
            resolve_media_playlist(
                video["source"]
            )
        )



        groups, total_duration = (
            build_balanced_segment_groups(
                media_playlist.segments,
                MAX_PART_DURATION_SECONDS,
            )
        )


        total_parts = len(groups)



        expected_tags = {
            make_part_tag(
                video_id,
                part_number,
                total_parts,
            )

            for part_number in range(
                1,
                total_parts + 1,
            )
        }



        missing_tags = (
            expected_tags
            - uploaded_part_tags
        )



        if not missing_tags:
            continue



        print(
            f"Duración total: "
            f"{format_seconds(total_duration)}. "
            f"Se generarán "
            f"{total_parts} parte(s)."
        )



        created_at = str(
            video.get(
                "created_at",
                "unknown",
            )
        )


        video_date = (
            created_at
            .split("T")[0]
            .split(" ")[0]
        )


        video_directory = (
            WORKSPACE
            / f"{video_date}_{video_id}"
        )


        video_directory.mkdir(
            parents=True,
            exist_ok=True,
        )



        (
            video_directory
            / "video.json"
        ).write_text(
            json.dumps(
                video,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )



        for part_number, group in enumerate(
            groups,
            start=1,
        ):


            part_tag = make_part_tag(
                video_id,
                part_number,
                total_parts,
            )



            if part_tag in uploaded_part_tags:

                print(
                    f"La parte {part_number}/{total_parts} ya existe; se omite."
                )

                continue



            playlist_path = (
                video_directory
                /
                (
                    f"playlist_part_"
                    f"{part_number:02d}_"
                    f"of_{total_parts:02d}.m3u8"
                )
            )


            output_path = (
                video_directory
                /
                (
                    f"part_"
                    f"{part_number:02d}_"
                    f"of_{total_parts:02d}.ts"
                )
            )



            print(
                f"Preparando parte "
                f"{part_number}/{total_parts}: "
                f"{format_seconds(group['duration'])}, "
                f"segmentos "
                f"{group['start'] + 1}-{group['end']}."
            )



            write_part_playlist(
                media_playlist,
                media_playlist_url,
                group["start"],
                group["end"],
                playlist_path,
            )



            try:

                download_part(
                    playlist_path,
                    output_path,
                )



                _, uploaded_tag = upload_video(
                    youtube,
                    video,
                    output_path,
                    part_number,
                    total_parts,
                )



                uploaded_part_tags.add(
                    uploaded_tag
                )



                # PAUSA ENTRE PARTES
                if part_number < total_parts:

                    print(
                        f"Parte {part_number}/{total_parts} subida. "
                        f"Esperando "
                        f"{UPLOAD_PAUSE_SECONDS} segundos "
                        f"antes de continuar..."
                    )


                    time.sleep(
                        UPLOAD_PAUSE_SECONDS
                    )



            finally:

                # Limpieza después de cada parte
                output_path.unlink(
                    missing_ok=True
                )

                playlist_path.unlink(
                    missing_ok=True
                )



        print(
            f"VOD {video_id} procesado completamente."
        )


        return True



    return False





def main():

    if PRIVACY_STATUS not in {
        "private",
        "unlisted",
        "public",
    }:

        raise RuntimeError(
            "YOUTUBE_PRIVACY_STATUS debe ser "
            "private, unlisted o public."
        )



    if MAX_PART_DURATION_SECONDS <= 0:

        raise RuntimeError(
            "MAX_PART_DURATION_SECONDS debe ser mayor que cero."
        )



    if UPLOAD_PAUSE_SECONDS < 0:

        raise RuntimeError(
            "UPLOAD_PAUSE_SECONDS no puede ser negativo."
        )



    WORKSPACE.mkdir(
        parents=True,
        exist_ok=True,
    )



    youtube = build_youtube_client()



    legacy_vod_ids, uploaded_part_tags = (
        get_uploaded_markers(
            youtube
        )
    )



    videos = get_kick_videos()



    (
        WORKSPACE
        / "videos.json"
    ).write_text(
        json.dumps(
            videos,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )



    processed = process_oldest_pending_video(
        youtube,
        videos,
        legacy_vod_ids,
        uploaded_part_tags,
    )



    if not processed:

        print(
            "No hay VODs pendientes para subir a YouTube."
        )



if __name__ == "__main__":

    main()
