import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("tool_result_parsers")

MAX_REFERENCE_TITLE_LENGTH = 120
MAX_REFERENCE_CONTENT_LENGTH = 100


class ToolResultParseError(ValueError):
    """Raised when a tool-specific parser cannot decode expected JSON."""


def _result_has_reference_section(text: str) -> bool:
    return isinstance(text, str) and "### Reference" in text


def _sanitize_reference_text(text: str, max_len: int) -> str:
    """Collapse whitespace/newlines and strip markdown that breaks list links."""
    if not text:
        return ""
    cleaned = " ".join(str(text).replace("\r", "\n").split())
    cleaned = cleaned.replace("```", "`").replace("[", "\\[").replace("]", "\\]")
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip(" .") + "..."
    return cleaned


def _format_references_markdown(references: list) -> str:
    """Build a Reference section safe for markdown list rendering."""
    lines = ["\n\n### Reference"]
    for i, reference in enumerate(references, start=1):
        title = _sanitize_reference_text(reference.get("title") or "Untitled", MAX_REFERENCE_TITLE_LENGTH)
        content = _sanitize_reference_text(reference.get("content") or "", MAX_REFERENCE_CONTENT_LENGTH)
        url = (reference.get("url") or "").strip()
        page = reference.get("page")
        page_suffix = f" , {page} page" if page is not None else ""
        if url:
            lines.append(
                f"{i}. [{title}]({url}){page_suffix} — {content}" if content
                else f"{i}. [{title}]({url}){page_suffix}"
            )
        else:
            lines.append(
                f"{i}. {title}{page_suffix} — {content}" if content
                else f"{i}. {title}{page_suffix}"
            )
    return "\n".join(lines) + "\n"


def _append_references_to_result(result, references: list):
    """Append a Reference block once; skip if the result already includes one."""
    if not references:
        return result
    text = result if isinstance(result, str) else (str(result) if result is not None else "")
    if _result_has_reference_section(text):
        return result
    return text + _format_references_markdown(references)



def _build_tool_reference(ref_item: dict) -> dict:
    """Build a display reference from a RAG doc item."""
    reference = ref_item.get("reference") or {}
    contents = ref_item.get("contents") or ""
    content_text = contents[:MAX_REFERENCE_CONTENT_LENGTH] + "..." if len(contents) > MAX_REFERENCE_CONTENT_LENGTH else contents
    result = {
        "url": reference.get("url"),
        "title": reference.get("title"),
        "content": content_text,
    }
    if reference.get("page") is not None:
        result["page"] = reference["page"]
    return result



def _extract_rag_references_from_payload(json_data) -> list:
    """Extract RAG {contents, reference} items from MCP tool payloads."""
    refs = []
    if isinstance(json_data, dict):
        if "reference" in json_data and "contents" in json_data:
            refs.append(_build_tool_reference(json_data))
            return refs
        for value in json_data.values():
            if isinstance(value, dict) and "reference" in value and "contents" in value:
                refs.append(_build_tool_reference(value))
        return refs

    if not isinstance(json_data, list):
        return refs

    for item in json_data:
        if not isinstance(item, dict):
            continue
        if "text" in item and ("type" not in item or item.get("type") == "text"):
            text_val = item.get("text")
            if isinstance(text_val, str):
                try:
                    text_json = json.loads(text_val)
                except (json.JSONDecodeError, TypeError):
                    continue
                refs.extend(_extract_rag_references_from_payload(text_json))
            continue
        if "reference" in item and "contents" in item:
            refs.append(_build_tool_reference(item))
    return refs


def _looks_like_tavily(tool_content) -> bool:
    return (
        isinstance(tool_content, str)
        and "Title:" in tool_content
        and "URL:" in tool_content
        and "Content:" in tool_content
    )


def _parse_tavily_result(tool_content):
    tool_references = []
    urls = []
    content = ""

    logger.info("Tavily parsing...")
    items = tool_content.split("\n\n")
    for i, item in enumerate(items):
        # logger.info(f"item[{i}]: {item}")
        if "Title:" in item and "URL:" in item and "Content:" in item:
            try:
                title_part = item.split("Title:")[1].split("URL:")[0].strip()
                url_part = item.split("URL:")[1].split("Content:")[0].strip()
                content_part = item.split("Content:")[1].strip().replace("\n", "")

                logger.info(f"title_part: {title_part}")
                logger.info(f"url_part: {url_part}")
                logger.info(f"content_part: {content_part}")

                content += f"{content_part}\n\n"

                tool_references.append({
                    "url": url_part,
                    "title": title_part,
                    "content": content_part[:MAX_REFERENCE_CONTENT_LENGTH] + "..." if len(content_part) > MAX_REFERENCE_CONTENT_LENGTH else content_part
                })
            except Exception as e:
                logger.info(f"Parsing error: {str(e)}")
                continue

    return content, urls, tool_references


def _parse_opensearch_result(tool_content):
    tool_references = []
    urls = []
    content = ""

    if ":" in tool_content:
        extracted_json_data = tool_content.split(":", 1)[1].strip()
        try:
            json_data = json.loads(extracted_json_data)
            # logger.info(f"extracted_json_data: {extracted_json_data[:200]}")
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse OpenSearch tool result as JSON", exc_info=True)
            raise ToolResultParseError("Failed to parse OpenSearch tool result") from e
    else:
        json_data = {}

    if "hits" in json_data:
        hits = json_data["hits"]["hits"]
        if hits:
            logger.info(f"hits[0]: {hits[0]}")

        for hit in hits:
            text = hit["_source"]["text"]
            metadata = hit["_source"]["metadata"]

            content += f"{text}\n\n"

            filename = metadata["name"].split("/")[-1]
            # logger.info(f"filename: {filename}")

            content_part = text.replace("\n", "")
            tool_references.append({
                "url": metadata["url"],
                "title": filename,
                "content": content_part[:MAX_REFERENCE_CONTENT_LENGTH] + "..." if len(content_part) > MAX_REFERENCE_CONTENT_LENGTH else content_part
            })

    logger.info(f"content: {content}")
    return content, urls, tool_references


def _parse_knowledge_base_result(tool_content):
    tool_references = []
    urls = []
    content = ""

    try:
        # Handle case where tool_content contains multiple JSON objects
        if tool_content.strip().startswith('{'):
            # Parse each JSON object individually
            json_objects = []
            brace_count = 0
            start_pos = -1

            for i, char in enumerate(tool_content):
                if char == '{':
                    if brace_count == 0:
                        start_pos = i
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0 and start_pos != -1:
                        try:
                            json_obj = json.loads(tool_content[start_pos:i+1])
                            # logger.info(f"json_obj: {json_obj}")
                            json_objects.append(json_obj)
                        except json.JSONDecodeError:
                            logger.warning(
                                "Failed to parse JSON object from knowledge base tool result: %s",
                                tool_content[start_pos:i+1][:MAX_REFERENCE_CONTENT_LENGTH],
                                exc_info=True,
                            )
                        start_pos = -1

            json_data = json_objects
        else:
            # Try original method
            json_data = json.loads(tool_content)
        # logger.info(f"json_data: {json_data}")

        # Build content
        if isinstance(json_data, list):
            for item in json_data:
                if isinstance(item, dict) and "content" in item:
                    content_text = item["content"].get("text", "")
                    content += content_text + "\n\n"

                    uri = ""
                    if "location" in item:
                        if "s3Location" in item["location"]:
                            uri = item["location"]["s3Location"]["uri"]

                            tool_references.append({
                                "url": url,
                                "title": uri.split("/")[-1],
                                "content": content_text[:MAX_REFERENCE_CONTENT_LENGTH] + "..." if len(content_text) > MAX_REFERENCE_CONTENT_LENGTH else content_text
                            })

    except json.JSONDecodeError as e:
        logger.warning("Failed to parse knowledge base tool result as JSON", exc_info=True)
        raise ToolResultParseError("Failed to parse knowledge base tool result") from e

    logger.info(f"content: {content}")
    logger.info(f"tool_references: {tool_references}")
    return content, urls, tool_references


def _parse_aws_docs_search_result(tool_content):
    tool_references = []
    urls = []
    content = ""

    try:
        # Handle case where tool_content is already a list (e.g., from toolResult)
        if isinstance(tool_content, list):
            # Extract text from list items if they have 'text' key
            json_data = []
            for item in tool_content:
                if isinstance(item, dict) and 'text' in item:
                    try:
                        parsed_text = json.loads(item['text'])
                        if isinstance(parsed_text, dict) and 'search_results' in parsed_text:
                            json_data = parsed_text['search_results']
                        elif isinstance(parsed_text, list):
                            json_data = parsed_text
                        else:
                            json_data.append(parsed_text)
                    except (json.JSONDecodeError, TypeError):
                        logger.info(f"Failed to parse text from list item: {item}")
                elif isinstance(item, dict):
                    json_data.append(item)
                else:
                    json_data.append(item)
        elif isinstance(tool_content, str):
            json_data = json.loads(tool_content)
        else:
            json_data = tool_content

        # Ensure json_data is iterable
        if not isinstance(json_data, list):
            json_data = [json_data]

        for item in json_data:
            logger.info(f"item: {item}")

            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except json.JSONDecodeError:
                    logger.info(f"Failed to parse item as JSON: {item}")
                    continue

            if isinstance(item, dict) and 'url' in item and 'title' in item:
                url = item['url']
                title = item['title']
                context_text = item.get('context', '')
                content_text = context_text[:MAX_REFERENCE_CONTENT_LENGTH] + "..." if len(context_text) > MAX_REFERENCE_CONTENT_LENGTH else context_text
                content += context_text + "\n\n"
                tool_references.append({
                    "url": url,
                    "title": title,
                    "content": content_text
                })
            else:
                logger.info(f"Invalid item format: {item}")

    except json.JSONDecodeError as e:
        logger.warning(
            "Failed to parse search_documentation tool result as JSON (type: %s)",
            type(tool_content),
            exc_info=True,
        )
        raise ToolResultParseError("Failed to parse search_documentation tool result") from e
    except Exception as e:
        logger.error("Error processing search_documentation", exc_info=True)
        raise ToolResultParseError("Failed to process search_documentation tool result") from e

    logger.info(f"content: {content}")
    logger.info(f"tool_references: {tool_references}")
    return content, urls, tool_references


def _parse_arxiv_result(tool_content):
    tool_references = []
    urls = []
    content = ""

    try:
        json_data = json.loads(tool_content)

        papers = json_data['papers']
        for paper in papers:
            url = paper['url']
            title = paper['title']
            abstract = paper['abstract'].replace("\n", "")
            content_text = abstract[:MAX_REFERENCE_CONTENT_LENGTH] + "..." if len(abstract) > MAX_REFERENCE_CONTENT_LENGTH else abstract
            content += f"{content_text}\n\n"
            logger.info(f"url: {url}, title: {title}, content: {content_text}")

            tool_references.append({
                "url": url,
                "title": title,
                "content": content_text
            })
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse arxiv tool result as JSON", exc_info=True)
        raise ToolResultParseError("Failed to parse arxiv tool result") from e

    logger.info(f"content: {content}")
    logger.info(f"tool_references: {tool_references}")
    return content, urls, tool_references


def _parse_aws_read_documentation_result(tool_content):
    tool_references = []
    urls = []
    content = ""

    logger.info(f"#### aws___read_documentation ####")
    if isinstance(tool_content, dict):
        json_data = tool_content
    elif isinstance(tool_content, list):
        json_data = tool_content
    else:
        json_data = json.loads(tool_content)

    logger.info(f"json_data: {json_data}")
    payload = json_data["response"]["payload"]
    if "content" in payload:
        payload_content = payload["content"]
        if "result" in payload_content:
            result = payload_content["result"]
            logger.info(f"result: {result}")
            if isinstance(result, str) and "AWS Documentation from" in result:
                logger.info(f"Processing AWS Documentation format: {result}")
                try:
                    # Extract URL from "AWS Documentation from https://..."
                    url_start = result.find("https://")
                    if url_start != -1:
                        # Find the colon after the URL (not inside the URL)
                        url_end = result.find(":", url_start)
                        if url_end != -1:
                            # Check if the colon is part of the URL or the separator
                            url_part = result[url_start:url_end]
                            # If the colon is immediately after the URL, use it as separator
                            if result[url_end:url_end+2] == ":\n":
                                url = url_part
                                content_start = url_end + 2  # Skip the colon and newline
                            else:
                                # Try to find the actual URL end by looking for space or newline
                                space_pos = result.find(" ", url_start)
                                newline_pos = result.find("\n", url_start)
                                if space_pos != -1 and newline_pos != -1:
                                    url_end = min(space_pos, newline_pos)
                                elif space_pos != -1:
                                    url_end = space_pos
                                elif newline_pos != -1:
                                    url_end = newline_pos
                                else:
                                    url_end = len(result)

                                url = result[url_start:url_end]
                                content_start = url_end + 1

                            # Remove trailing colon from URL if present
                            if url.endswith(":"):
                                url = url[:-1]

                            # Extract content after the URL
                            if content_start < len(result):
                                content_text = result[content_start:].strip()
                                # Truncate content for display
                                display_content = content_text[:MAX_REFERENCE_CONTENT_LENGTH] + "..." if len(content_text) > MAX_REFERENCE_CONTENT_LENGTH else content_text
                                display_content = display_content.replace("\n", "")

                                tool_references.append({
                                    "url": url,
                                    "title": "AWS Documentation",
                                    "content": display_content
                                })
                                content += content_text + "\n\n"
                                logger.info(f"Extracted URL: {url}")
                                logger.info(f"Extracted content length: {len(content_text)}")
                except Exception as e:
                    logger.error(f"Error parsing AWS Documentation format: {e}")
    logger.info(f"content: {content}")
    logger.info(f"tool_references: {tool_references}")
    return content, urls, tool_references


def _parse_generic_tool_result(tool_content):
    tool_references = []
    urls = []
    content = ""

    try:
        if isinstance(tool_content, dict):
            json_data = tool_content
        elif isinstance(tool_content, list):
            json_data = tool_content
        else:
            json_data = json.loads(tool_content)

        logger.info(f"json_data: {json_data}")
        if isinstance(json_data, dict) and "path" in json_data:  # path
            path = json_data["path"]
            if isinstance(path, list):
                for url in path:
                    urls.append(url)
            else:
                urls.append(path)

        # RAG retrieve returns MCP blocks: [{"type":"text","text":"[{contents,reference}...]"}]
        extracted = _extract_rag_references_from_payload(json_data)
        tool_references.extend(extracted)

        logger.info(f"tool_references: {tool_references}")

    except json.JSONDecodeError:
        # tool_content is plain text (not JSON) for most tools; this is the
        # expected fallback path, so log at debug level to avoid log spam.
        logger.debug("Generic tool result is not JSON; no references extracted")

    return content, urls, tool_references


def get_tool_info(tool_name, tool_content):
    """Dispatch tool-result parsing to a tool-specific parser."""
    if _looks_like_tavily(tool_content):
        return _parse_tavily_result(tool_content)
    if tool_name == "SearchIndexTool":
        return _parse_opensearch_result(tool_content)
    if tool_name == "QueryKnowledgeBases":
        return _parse_knowledge_base_result(tool_content)
    if tool_name == "search_documentation":
        return _parse_aws_docs_search_result(tool_content)
    if tool_name == "search_papers" and "papers" in tool_content:
        return _parse_arxiv_result(tool_content)
    if tool_name == "aws___read_documentation":
        return _parse_aws_read_documentation_result(tool_content)
    return _parse_generic_tool_result(tool_content)
