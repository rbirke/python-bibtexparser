import logging
import re
from typing import Dict, List, Optional, Set, Tuple, Union

from bibtexparser.exceptions import (
    BlockAbortedException,
    ParserStateException,
    RegexMismatchException,
)
from bibtexparser.library import Library
from bibtexparser.model import (
    DuplicateFieldKeyBlock,
    Entry,
    ExplicitComment,
    Field,
    ImplicitComment,
    ParsingFailedBlock,
    Preamble,
    String,
)


class Splitter:
    def __init__(self, bibstr: str):
        # Add a newline at the beginning to simplify parsing
        #   (we only allow "@"-block starts after a newline)
        self.bibstr = f"\n{bibstr}"

        self._markiter = None
        self._unaccepted_mark = None

        # Keep track of line we're currently looking at.
        #   `-1` compensates for manually added `\n` above
        self._current_line = -1

        self._reset_block_status(current_char_index=0)

    def _reset_block_status(self, current_char_index):
        self._open_brackets = 0
        self._is_quote_open = False
        self._expected_next: Optional[List[str]] = None

        # By default, we assume that an implicit comment is started
        #   at the beginning of the file and after each @{...} block.
        #   We then ignore empty implicit comments.
        self._implicit_comment_start_line = self._current_line
        self._implicit_comment_start: Optional[int] = current_char_index

    def _end_implicit_comment(self, end_char_index) -> Optional[ImplicitComment]:
        if self._implicit_comment_start is None:
            return  # No implicit comment started

        comment = self.bibstr[self._implicit_comment_start : end_char_index]

        # Clear leading and trailing empty lines,
        #   and count how many lines were removed, to adapt start_line below
        leading_empty_lines = 0
        i = 0
        for i, char in enumerate(comment):
            if char == "\n":
                leading_empty_lines += 1
            elif not char.isspace():
                break

        comment = comment[i:].rstrip()

        if len(comment) > 0:
            return ImplicitComment(
                start_line=self._implicit_comment_start_line + leading_empty_lines,
                raw=comment,
                comment=comment,
            )
        else:
            return None

    def _next_mark(self, accept_eof: bool) -> Optional[re.Match]:
        # Check if there is a mark that was previously not consumed
        #   and return it if so
        if self._unaccepted_mark is not None:
            m = self._unaccepted_mark
            self._unaccepted_mark = None
            self._current_char_index = m.start()
            return m

        # Get next mark from iterator
        m = next(self._markiter, None)
        if m is not None:
            self._current_char_index = m.start()
            if m.group(0) == "\n":
                self._current_line += 1
                return self._next_mark(accept_eof=accept_eof)
        else:
            # Reached end of file
            self._current_char_index = len(self.bibstr)
            if not accept_eof:
                raise BlockAbortedException(
                    abort_reason="Unexpectedly reached end of file.",
                    end_index=self._current_char_index,
                )
        return m

    def _move_to_closed_bracket(self) -> int:
        """Index of the curly bracket closing a just opened one."""
        num_additional_brackets = 0
        while True:
            m = self._next_mark(accept_eof=False)
            if m.group(0) == "{":
                num_additional_brackets += 1
            elif m.group(0) == "}":
                if num_additional_brackets == 0:
                    return m.start()
                else:
                    num_additional_brackets -= 1
            elif m.group(0).startswith("@"):
                self._unaccepted_mark = m
                raise BlockAbortedException(
                    abort_reason=f"Unexpected block start: `{m.group(0)}`. "
                    f"Was still looking for closing bracket",
                    end_index=m.start() - 1,
                )

    def _move_to_end_of_double_quoted_string(self) -> int:
        """Index of the closing double quote."""
        while True:
            m = self._next_mark(accept_eof=False)

            if m.group(0) == '"':
                return m.start()
            elif m.group(0).startswith("@"):
                self._unaccepted_mark = m
                raise BlockAbortedException(
                    abort_reason=f"Unexpected block start: `{m.group(0)}`. "
                    f'Was still looking for field-value closing `"`',
                    end_index=m.start() - 1,
                )

    def _move_to_end_of_entry(
        self, first_key_start: int
    ) -> Tuple[Dict[str, Field], int, Set[str]]:
        """Move to the end of the entry and return the fields and the end index."""
        result = dict()
        duplicate_keys = set()

        key_start = first_key_start
        while True:
            equals_mark = self._next_mark(accept_eof=False)
            if equals_mark.group(0) == "}":
                # End of entry
                return result, equals_mark.end(), duplicate_keys

            if equals_mark.group(0) != "=":
                self._unaccepted_mark = equals_mark
                raise BlockAbortedException(
                    abort_reason="Expected a `=` after entry key, "
                    f"but found `{equals_mark.group(0)}`.",
                    end_index=equals_mark.start(),
                )

            # We follow the convention that the field start line
            #   is where the `=` between key and value is.
            start_line = self._current_line
            key_end = equals_mark.start()
            value_start = equals_mark.end()
            value_start_mark = self._next_mark(accept_eof=False)

            if value_start_mark.group(0) == "{":
                value_end = self._move_to_closed_bracket() + 1
            elif value_start_mark.group(0) == '"':
                value_end = self._move_to_end_of_double_quoted_string() + 1
            else:
                # e.g.  String reference or integer. Ended by the observed mark
                #       (as there is not start mark).
                #       Should be either a comma or a "}"
                value_start = equals_mark.end()
                value_end = value_start_mark.start()
                # We expect a comma (after a closed field-value), or at the end of entry, a closing bracket
                if not value_start_mark.group(0) in [
                    ",",
                    "}",
                ]:
                    self._unaccepted_mark = value_start_mark
                    raise BlockAbortedException(
                        abort_reason=f"Unexpected character `{value_start_mark.group(0)}` "
                        f"after field-value. Expected a comma or closing bracket.",
                        end_index=value_start_mark.start(),
                    )
                # Put comma back into stream, as still expected.
                self._unaccepted_mark = value_start_mark

            key = self.bibstr[key_start:key_end].strip()
            value = self.bibstr[value_start:value_end].strip()

            if key in result:
                duplicate_keys.add(key)
                duplicate_count = 1
                while f"{key}_duplicate_{duplicate_count}" in result:
                    duplicate_count += 1

                key = f"{key}_duplicate_{duplicate_count}"

            result[key] = Field(start_line=start_line, key=key, value=value)

            # If next mark is a comma, continue
            after_field_mark = self._next_mark(accept_eof=False)
            if after_field_mark.group(0) == ",":
                key_start = after_field_mark.end()
            elif after_field_mark.group(0) == "}":
                # If next mark is a closing bracket, put it back (will return in next loop iteration)
                self._unaccepted_mark = after_field_mark
                continue
            else:
                self._unaccepted_mark = after_field_mark
                raise BlockAbortedException(
                    abort_reason="Expected either a `,` or `}` after a closed entry field value, "
                    f"but found a {after_field_mark.group(0)} before.",
                    end_index=after_field_mark.start(),
                )

    def split(self, library: Optional[Library] = None) -> Library:
        self._markiter = re.finditer(
            r"(?<!\\)[\{\}\",=\n]|(?<=\n)@[\w]*(?={)", self.bibstr, re.MULTILINE
        )

        if library is None:
            library = Library()
        else:
            logging.info("Adding blocks to existing library.")

        while True:
            m = self._next_mark(accept_eof=True)
            if m is None:
                break

            m_val = m.group(0).lower()

            if m_val.startswith("@"):
                # Clean up previous block implicit_comment
                implicit_comment = self._end_implicit_comment(m.start())
                if implicit_comment is not None:
                    library.add(implicit_comment)
                self._implicit_comment_start = None

                start_line = self._current_line
                try:
                    # Start new block parsing
                    if m_val.startswith("@comment"):
                        library.add(self._handle_explicit_comment())
                    elif m_val.startswith("@preamble"):
                        library.add(self._handle_preamble())
                    elif m_val.startswith("@string"):
                        library.add(self._handle_string(m))
                    else:
                        library.add(self._handle_entry(m, m_val))

                except BlockAbortedException as e:
                    logging.warning(
                        f"Parsing of `{m_val}` block (line {start_line}) aborted on line {self._current_line}  "
                        f"due to syntactical error in bibtex:\n {e.abort_reason}"
                    )
                    logging.info(
                        "We will try to continue parsing, but this might lead to unexpected results."
                        "The failed block will be stored in the `failed_blocks`of the library."
                    )
                    library.add(
                        ParsingFailedBlock(
                            start_line=start_line,
                            raw=self.bibstr[m.start() : e.end_index],
                            error=e,
                        )
                    )

                except ParserStateException as e:
                    logging.error(e.message)
                    raise e  # TODO consider allowing to continue
                except Exception as e:
                    logging.error(
                        f"Unexpected exception while parsing `{m_val}` block (line {start_line})"
                    )
                    raise e  # TODO consider allowing to continue

                self._reset_block_status(
                    current_char_index=self._current_char_index + 1
                )
            else:
                # Part of implicit comment
                continue

        # Check if there's an implicit comment at the EOF
        if self._implicit_comment_start is not None:
            comment = self._end_implicit_comment(len(self.bibstr))
            if comment is not None:
                library.add(comment)

        return library

    def _handle_explicit_comment(self) -> ExplicitComment:
        """Handle explicit comment block. Return end index"""
        start_index = self._current_char_index
        start_line = self._current_line
        start_bracket_mark = self._next_mark(accept_eof=False)
        if start_bracket_mark.group(0) != "{":
            self._unaccepted_mark = start_bracket_mark
            # Note: The following should never happen, as we check for the "{" in the regex
            raise RegexMismatchException(
                first_match="@comment{",
                expected_match="{",
                second_match=start_bracket_mark.group(0),
            )
        end_bracket_index = self._move_to_closed_bracket()
        comment_str = self.bibstr[start_bracket_mark.end() : end_bracket_index].strip()
        return ExplicitComment(
            start_line=start_line,
            comment=comment_str,
            raw=self.bibstr[start_index : end_bracket_index + 1],
        )

    def _handle_entry(self, m, m_val) -> Union[Entry, ParsingFailedBlock]:
        """Handle entry block. Return end index"""
        start_line = self._current_line
        entry_type = m_val[1:]
        start_bracket_mark = self._next_mark(accept_eof=False)
        if start_bracket_mark.group(0) != "{":
            self._unaccepted_mark = start_bracket_mark
            # Note: The following should never happen, as we check for the "{" in the regex
            raise ParserStateException(
                message="matched a regex that should end with `{`, "
                "e.g. `@article{`, "
                "but no closing bracket was found."
            )
        comma_mark = self._next_mark(accept_eof=False)
        if comma_mark.group(0) != ",":
            self._unaccepted_mark = comma_mark
            raise BlockAbortedException(
                abort_reason="Expected comma after entry key,"
                f" but found {comma_mark.group(0)}",
                end_index=comma_mark.end(),
            )
        self._open_brackets += 1
        key = self.bibstr[m.end() + 1 : comma_mark.start()].strip()
        fields, end_index, duplicate_keys = self._move_to_end_of_entry(comma_mark.end())

        entry = Entry(
            start_line=start_line,
            entry_type=entry_type,
            key=key,
            fields=fields,
            raw=self.bibstr[m.start() : end_index + 1],
        )

        # If there were duplicate field keys, we return a DuplicateFieldKeyBlock wrapping
        if len(duplicate_keys) > 0:
            return DuplicateFieldKeyBlock(duplicate_keys=duplicate_keys, entry=entry)
        else:
            return entry

    def _handle_string(self, m) -> String:
        """Handle string block. Return end index"""
        # Get next mark, which should be an equals sign
        start_i = self._current_char_index
        start_line = self._current_line
        start_bracket_mark = self._next_mark(accept_eof=False)
        if start_bracket_mark.group(0) != "{":
            self._unaccepted_mark = start_bracket_mark
            # Note: The following should never happen, as we check for the "{" in the regex
            raise ParserStateException(
                message="matched a string def regex (`@string{`) that "
                "should end with `{`, but no closing bracket was found."
            )
        equals_mark = self._next_mark(accept_eof=False)
        if equals_mark.group(0) != "=":
            self._unaccepted_mark = equals_mark
            raise BlockAbortedException(
                abort_reason="Expected equals sign after field key,"
                f" but found {equals_mark.group(0)}",
                end_index=equals_mark.end(),
            )
        key = self.bibstr[m.end() + 1 : equals_mark.start()].strip()
        value_start = equals_mark.end()
        end_i = self._move_to_closed_bracket()
        value = self.bibstr[value_start:end_i].strip()
        return String(
            start_line=start_line,
            key=key,
            value=value,
            raw=self.bibstr[start_i : end_i + 1],
        )

    def _handle_preamble(self) -> Preamble:
        """Handle preamble block. Return end index"""
        start_i = self._current_char_index
        start_line = self._current_line
        start_bracket_mark = self._next_mark(accept_eof=False)
        if start_bracket_mark.group(0) != "{":
            self._unaccepted_mark = start_bracket_mark
            # Note: The following should never happen, as we check for the "{" in the regex
            raise ParserStateException(
                message="matched a preamble def regex (`@preamble{`) that "
                "should end with `{`, but no closing bracket was found."
            )

        end_bracket_index = self._move_to_closed_bracket()
        preamble = self.bibstr[start_bracket_mark.end() : end_bracket_index]
        return Preamble(
            start_line=start_line,
            value=preamble,
            raw=self.bibstr[start_i : end_bracket_index + 1],
        )